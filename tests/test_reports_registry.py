"""The report sources are plugins: a `ReportSource` subclass in a module under a package is
found, by provider, with nothing registered by hand."""

from __future__ import annotations

import json
import sys
import textwrap
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from data_sources.config.schema import AuthConfig, ConnectorConfig
from data_sources.core.exceptions import ConnectorNotFoundError
from data_sources.reports import (
    Account,
    GoogleReportSource,
    Grant,
    RawReport,
    ReportSource,
    ReportSourceRegistry,
    create_report_source,
    discover,
    report_sources,
)
from data_sources.reports.oauth import GOOGLE_TOKEN_URL

PROVIDERS = [
    "facebook_pages",
    "google_ads",
    "google_analytics",
    "google_merchant",
    "google_search_console",
    "instagram",
    "meta_ads",
    "tiktok_ads",
    "tiktok_organic",
]

PLUGIN = """
from data_sources.reports.base import Account, Grant, ReportSource
from data_sources.reports.models import RawReport


class {name}(ReportSource):
    provider = "{provider}"
    app_credentials = ("key",)

    def authorize_url(self, state):
        return f"https://example.com/auth?state={{state}}"

    async def exchange_code(self, code):
        return Grant({{"token": code}})

    async def list_accounts(self, credentials):
        return [Account("a1", "Account 1")]

    async def read(self, credentials, account, spec, since, until):
        return RawReport([{{"account": account}}], [since])
"""


@pytest.fixture
def plugins(tmp_path: Path) -> Iterator[Path]:
    """An importable, empty package to drop source modules into."""
    name = f"plugins_{tmp_path.name}"
    package = tmp_path / name
    package.mkdir()
    (package / "__init__.py").write_text("")
    sys.path.insert(0, str(tmp_path))
    yield package
    sys.path.remove(str(tmp_path))
    for module in [m for m in sys.modules if m == name or m.startswith(f"{name}.")]:
        del sys.modules[module]


def drop(package: Path, module: str, body: str) -> None:
    (package / f"{module}.py").write_text(textwrap.dedent(body))


def test_every_built_in_provider_is_discovered() -> None:
    assert report_sources.providers() == PROVIDERS
    assert sorted(discover()) == PROVIDERS


def test_a_dropped_in_file_is_a_new_source(plugins: Path) -> None:
    drop(plugins, "acme", PLUGIN.format(name="AcmeReports", provider="acme"))
    registry = ReportSourceRegistry(plugins.name)
    assert registry.providers() == ["acme"]
    source = registry.create(
        ConnectorConfig(provider="acme", auth=AuthConfig(type="oauth2", credentials={"key": "k"}))
    )
    assert type(source).__name__ == "AcmeReports" and source.configured
    assert not registry.create(ConnectorConfig(provider="acme")).configured


def test_sub_packages_are_walked_and_abstract_bases_and_imports_skipped(plugins: Path) -> None:
    nested = plugins / "nested"
    nested.mkdir()
    (nested / "__init__.py").write_text("")
    drop(nested, "deep", PLUGIN.format(name="DeepReports", provider="deep"))
    # A shared abstract base, and a module importing another's class, register nothing more.
    drop(
        plugins,
        "shared",
        "from data_sources.reports.google import GoogleReportSource\n"
        f"from {plugins.name}.nested.deep import DeepReports\n",
    )
    assert sorted(discover(plugins.name)) == ["deep"]


def test_two_sources_for_one_provider_fail_loudly(plugins: Path) -> None:
    drop(plugins, "one", PLUGIN.format(name="One", provider="same"))
    drop(plugins, "two", PLUGIN.format(name="Two", provider="same"))
    with pytest.raises(RuntimeError, match="Two report sources claim 'same'"):
        discover(plugins.name)


def test_a_source_that_fails_to_import_fails_loudly(plugins: Path) -> None:
    drop(plugins, "broken", "import not_a_module_anywhere\n")
    with pytest.raises(ImportError):
        ReportSourceRegistry(plugins.name).providers()


def test_unknown_provider() -> None:
    with pytest.raises(ConnectorNotFoundError, match="nope"):
        create_report_source(ConnectorConfig(provider="nope"))


def test_register_and_unregister_by_hand(plugins: Path) -> None:
    drop(plugins, "acme", PLUGIN.format(name="AcmeReports", provider="acme"))
    registry = ReportSourceRegistry(plugins.name)
    cls = registry.get("acme")
    registry.register("other", cls)
    assert registry.providers() == ["acme", "other"]
    registry.unregister("other")
    assert registry.providers() == ["acme"]


def test_google_base_is_not_a_source() -> None:
    assert GoogleReportSource not in report_sources._loaded.values()
    assert all(issubclass(report_sources.get(p), ReportSource) for p in PROVIDERS)


@pytest.mark.parametrize(
    ("provider", "credentials", "configured"),
    [
        ("google_analytics", {"client_id": "c", "client_secret": "s"}, True),
        ("google_analytics", {"client_id": "c"}, False),
        ("google_ads", {"client_id": "c", "client_secret": "s"}, True),
        ("google_ads", {"client_secret": "s", "developer_token": "d"}, False),
        ("meta_ads", {"app_id": "a", "app_secret": "s"}, True),
        ("tiktok_ads", {"app_id": "a"}, False),
    ],
)
def test_configured_needs_every_app_credential(
    provider: str, credentials: dict[str, str], configured: bool
) -> None:
    config = ConnectorConfig(
        provider=provider, auth=AuthConfig(type="oauth2", credentials=credentials)
    )
    assert create_report_source(config).configured is configured


# --- the uniform interface, per provider -------------------------------------------------------


def source(provider: str, handler: Any, **credentials: str) -> ReportSource:
    async def no_wait(_: float) -> None:
        return None

    config = ConnectorConfig(
        provider=provider,
        auth=AuthConfig(
            type="oauth2",
            credentials={"redirect_uri": "https://app.example/cb", **credentials},
        ),
    )
    return create_report_source(config, transport=httpx.MockTransport(handler), sleep=no_wait)


GOOGLE = {"client_id": "cid", "client_secret": "secret"}


def google(handler: Any) -> Any:
    def wrapped(request: httpx.Request) -> httpx.Response:
        if str(request.url) == GOOGLE_TOKEN_URL:
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                return httpx.Response(200, json={"refresh_token": "r1"})
            assert form["refresh_token"] == ["r1"]
            return httpx.Response(200, json={"access_token": "a1"})
        return handler(request)

    return wrapped


async def test_google_analytics_uniform() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "accountSummaries" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "accountSummaries": [
                        {
                            "account": "accounts/1",
                            "displayName": "Acme",
                            "propertySummaries": [
                                {"property": "properties/9", "displayName": "Shop"}
                            ],
                        }
                    ]
                },
            )
        assert json.loads(request.content)["dimensions"] == [{"name": "date"}]
        return httpx.Response(
            200,
            json={
                "dimensionHeaders": [{"name": "date"}],
                "rows": [{"dimensionValues": [{"value": "20261001"}], "metricValues": []}],
                "rowCount": 1,
            },
        )

    ga = source("google_analytics", google(handler), **GOOGLE)
    grant = await ga.exchange_code("code")
    assert grant == Grant({"refresh_token": "r1"})
    accounts = await ga.list_accounts(grant.credentials)
    assert accounts == [
        Account("properties/9", "Shop", group="Acme", details={"account": "accounts/1"})
    ]
    report = await ga.read(
        grant.credentials,
        "properties/9",
        {"dimensions": ["date"], "metrics": ["sessions"]},
        date(2026, 10, 1),
        date(2026, 10, 1),
    )
    assert report.days == [date(2026, 10, 1)]


async def test_google_ads_account_names_its_manager_route() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("listAccessibleCustomers"):
            return httpx.Response(200, json={"resourceNames": ["customers/1111111111"]})
        query = json.loads(request.content)["query"]
        if "FROM customer_client" in query:
            assert request.headers["login-customer-id"] == "1111111111"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"customerClient": {"id": "2222222222", "descriptiveName": "Child"}}
                    ]
                },
            )
        if "FROM customer" in query:
            return httpx.Response(
                200, json={"results": [{"customer": {"id": "1111111111", "manager": True}}]}
            )
        assert request.headers["login-customer-id"] == "1111111111"
        assert "/customers/2222222222/" in request.url.path
        return httpx.Response(200, json={"results": []})

    ads = source("google_ads", google(handler), developer_token="dev", **GOOGLE)
    credentials = {"refresh_token": "r1"}
    [account] = await ads.list_accounts(credentials)
    assert (account.id, account.name, account.group) == (
        "2222222222:1111111111",
        "Child",
        "1111111111",
    )
    report = await ads.read(
        credentials,
        account.id,
        {"resource": "campaign", "fields": ["campaign.id", "segments.date"]},
        date(2026, 10, 1),
        date(2026, 10, 2),
    )
    assert report.records == [] and report.envelope["login_customer_id"] == "1111111111"


async def test_search_console_uniform() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"siteEntry": [{"siteUrl": "sc-domain:x.ro", "permissionLevel": "siteOwner"}]},
            )
        return httpx.Response(200, json={"rows": []})

    sc = source("google_search_console", google(handler), **GOOGLE)
    credentials = {"refresh_token": "r1"}
    assert await sc.list_accounts(credentials) == [
        Account("sc-domain:x.ro", "sc-domain:x.ro", details={"permission_level": "siteOwner"})
    ]
    report = await sc.read(
        credentials,
        "sc-domain:x.ro",
        {"dimensions": ["date"]},
        date(2026, 10, 1),
        date(2026, 10, 1),
    )
    assert isinstance(report, RawReport) and report.envelope["siteUrl"] == "sc-domain:x.ro"


async def test_meta_uniform() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("oauth/access_token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 60})
        if request.url.path.endswith("/me"):
            return httpx.Response(200, json={"name": "Ana"})
        if request.url.path.endswith("adaccounts"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "act_1",
                            "name": "Ads",
                            "currency": "RON",
                            "account_status": 1,
                            "timezone_name": "Europe/Bucharest",
                        }
                    ]
                },
            )
        assert request.url.params["level"] == "campaign"
        return httpx.Response(200, json={"data": [{"date_start": "2026-10-01"}]})

    meta = source("meta_ads", handler, app_id="app", app_secret="s")
    grant = await meta.exchange_code("c")
    assert grant == Grant({"access_token": "t"}, identity="Ana", expires_in=60)
    assert await meta.list_accounts(grant.credentials) == [
        Account("act_1", "Ads", currency="RON", timezone="Europe/Bucharest", details={"status": 1})
    ]
    report = await meta.read(
        grant.credentials,
        "act_1",
        {"fields": ["spend"], "level": "campaign"},
        date(2026, 10, 1),
        date(2026, 10, 1),
    )
    assert report.days == [date(2026, 10, 1)]


async def test_tiktok_uniform() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("access_token/"):
            data: dict[str, Any] = {"access_token": "t", "advertiser_ids": ["7"]}
        elif request.url.path.endswith("advertiser/info/"):
            data = {"list": [{"advertiser_id": "7", "name": "Adv", "currency": "EUR"}]}
        else:
            assert request.url.params["data_level"] == "AUCTION_CAMPAIGN"
            data = {"list": [], "page_info": {"page": 1, "total_page": 0}}
        return httpx.Response(200, json={"code": 0, "data": data})

    tiktok = source("tiktok_ads", handler, app_id="app", app_secret="s")
    grant = await tiktok.exchange_code("c")
    assert grant.credentials == {"access_token": "t", "advertiser_ids": ["7"]}
    assert await tiktok.list_accounts(grant.credentials) == [Account("7", "Adv", currency="EUR")]
    report = await tiktok.read(
        grant.credentials,
        "7",
        {
            "data_level": "AUCTION_CAMPAIGN",
            "dimensions": ["campaign_id", "stat_time_day"],
            "metrics": ["spend"],
        },
        date(2026, 10, 1),
        date(2026, 10, 1),
    )
    assert report.records == [] and report.envelope == {"advertiser_id": "7"}
