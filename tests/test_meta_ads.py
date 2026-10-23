from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest

from data_sources.config.schema import AuthConfig, ConnectorConfig
from data_sources.connectors.meta_ads import (
    InsightsSpec,
    MetaAdsClient,
    MetaAdsConnector,
)
from data_sources.connectors.meta_ads.connector import parse_ad_account
from data_sources.connectors.meta_ads.oauth import (
    authorize_url,
    exchange_code,
    extend_token,
)
from data_sources.core.exceptions import (
    AuthenticationError,
    ConfigurationError,
    ConnectionError,
    DataSourceError,
    NotFoundError,
    RateLimitError,
)
from data_sources.core.models import ItemType
from data_sources.core.registry import registry

TOKEN = "tok-123"
ROWS: list[dict[str, Any]] = [
    {
        "campaign_name": "Spring",
        "spend": "10.50",
        "actions": [{"action_type": "click", "value": "3"}],
    },
    {"campaign_name": "Summer, EU", "spend": "7.00", "date_start": "2026-10-01"},
    {"campaign_name": "Autumn", "spend": "1.25"},
]
REPORTS = {"spend": {"fields": ["campaign_name", "spend"], "level": "campaign"}}


def graph_error(code: int, message: str = "boom", status: int = 400) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}})


class FakeMeta:
    """The Graph API behind `httpx.MockTransport`: ad accounts, and one insights table served
    in cursor pages (`after` is an offset)."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.url.path.startswith("/v26.0/")
        path = request.url.path.removeprefix("/v26.0/")
        params = request.url.params
        if path == "me/adaccounts":
            return self.ad_accounts(params.get("after"))
        if path == "act_123":
            return httpx.Response(200, json={"id": "act_123"})
        if path == "act_123/insights":
            offset = int(params.get("after") or 0)
            limit = int(params["limit"])
            page = self.rows[offset : offset + limit]
            body: dict[str, Any] = {"data": page}
            if offset + limit < len(self.rows):
                body["paging"] = {"cursors": {"after": str(offset + limit)}, "next": "https://x"}
            return httpx.Response(200, json=body)
        return graph_error(803, "unknown", 400)

    @staticmethod
    def ad_accounts(after: str | None) -> httpx.Response:
        if after == "c1":
            return httpx.Response(
                200, json={"data": [{"id": "act_2", "name": "Beta", "account_status": 2}]}
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "act_1",
                        "name": "Alpha",
                        "currency": "EUR",
                        "account_status": 1,
                        "timezone_name": "Europe/Bucharest",
                    }
                ],
                "paging": {"cursors": {"after": "c1"}, "next": "https://x"},
            },
        )


def client_for(fake: FakeMeta, **kwargs: Any) -> MetaAdsClient:
    return MetaAdsClient(
        TOKEN, http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)), **kwargs
    )


def make(
    fake: FakeMeta,
    *,
    page_size: int = 100,
    reports: Any = REPORTS,
    account: str | None = "123",
) -> MetaAdsConnector:
    options: dict[str, Any] = {"reports": reports, "page_size": page_size}
    if account:
        options["ad_account"] = account
    connector = MetaAdsConnector(ConnectorConfig(provider="meta_ads", options=options))
    connector._client = client_for(fake)
    return connector


def test_registered() -> None:
    assert registry.get("meta_ads") is MetaAdsConnector


@pytest.mark.parametrize("value", ["123", "act_123"])
def test_parse_ad_account(value: str) -> None:
    assert parse_ad_account(value) == "act_123"


@pytest.mark.parametrize("value", ["", "act_", "abc", "1/2"])
def test_parse_ad_account_rejects_others(value: str) -> None:
    with pytest.raises(ConfigurationError):
        parse_ad_account(value)


@pytest.mark.parametrize(
    "options",
    [
        {"ad_account": "bad"},
        {"reports": "nope"},
        {"reports": {"r": {"fields": []}}},
        {"reports": {"a/b": {"fields": ["spend"]}}},
        {"api_version": "26"},
        {"page_size": 0},
    ],
)
def test_bad_config(options: dict[str, Any]) -> None:
    with pytest.raises(ConfigurationError):
        MetaAdsConnector(ConnectorConfig(provider="meta_ads", options=options))


@pytest.mark.parametrize(
    "auth",
    [
        None,
        AuthConfig(type="oauth", credentials={"access_token": "x"}),
        AuthConfig(type="access_token", credentials={}),
    ],
)
async def test_connect_needs_an_access_token(auth: AuthConfig | None) -> None:
    connector = MetaAdsConnector(ConnectorConfig(provider="meta_ads", auth=auth))
    with pytest.raises(ConfigurationError):
        await connector.connect()


async def test_connect_and_close() -> None:
    connector = MetaAdsConnector(
        ConnectorConfig(
            provider="meta_ads",
            auth=AuthConfig(type="access_token", credentials={"access_token": TOKEN}),
        )
    )
    await connector.connect()
    await connector.close()
    with pytest.raises(ConnectionError):
        _ = connector.client


async def test_validate() -> None:
    assert await make(FakeMeta([])).validate() is True
    assert await make(FakeMeta([]), account=None).validate() is True
    assert await make(FakeMeta([]), account="999").validate() is False


async def test_list_ad_accounts_reads_every_page() -> None:
    found = await make(FakeMeta([]), account=None).list_ad_accounts()

    assert [(a.account, a.name, a.currency, a.status) for a in found] == [
        ("act_1", "Alpha", "EUR", 1),
        ("act_2", "Beta", "", 2),
    ]
    assert found[0].timezone == "Europe/Bucharest"


async def test_without_an_account_only_discovery_works() -> None:
    connector = make(FakeMeta([]), account=None, reports={})
    with pytest.raises(ConfigurationError, match="list_ad_accounts"):
        await connector.run_insights({"fields": ["spend"]})
    assert [i async for i in connector.list()] == []  # no reports configured, nothing to ask


async def test_run_insights_pages_and_flattens() -> None:
    fake = FakeMeta(ROWS)
    result = await make(fake, page_size=2).run_insights(
        {"fields": ["campaign_name", "spend", "actions"], "level": "campaign"}
    )

    assert result.headers == ["campaign_name", "spend", "actions", "date_start"]
    assert result.rows[0] == ["Spring", "10.50", '[{"action_type":"click","value":"3"}]', ""]
    assert result.rows[1] == ["Summer, EU", "7.00", "", "2026-10-01"]
    assert len(result.rows) == 3
    assert [r.url.params.get("after") for r in fake.requests] == [None, "2"]


async def test_run_insights_sends_the_spec() -> None:
    fake = FakeMeta([])
    await make(fake).run_insights(
        {
            "fields": ["spend"],
            "level": "ad",
            "since": "2026-09-01",
            "until": "2026-09-30",
            "time_increment": 1,
            "breakdowns": ["age", "gender"],
            "filtering": [{"field": "spend", "operator": "GREATER_THAN", "value": "0"}],
            "sort": ["spend_descending"],
        }
    )
    params = fake.requests[0].url.params

    assert params["level"] == "ad"
    assert json.loads(params["time_range"]) == {"since": "2026-09-01", "until": "2026-09-30"}
    assert "date_preset" not in params
    assert params["time_increment"] == "1"
    assert params["breakdowns"] == "age,gender"
    assert json.loads(params["filtering"])[0]["operator"] == "GREATER_THAN"
    assert params["sort"] == "spend_descending"


async def test_run_insights_defaults_to_last_30_days() -> None:
    fake = FakeMeta([])
    await make(fake).run_insights({"fields": ["spend"]})
    assert fake.requests[0].url.params["date_preset"] == "last_30d"


async def test_run_insights_limit_caps_rows_and_request_size() -> None:
    fake = FakeMeta(ROWS)
    result = await make(fake, page_size=2).run_insights({"fields": ["spend"], "limit": 3})

    assert len(result.rows) == 3
    assert [r.url.params["limit"] for r in fake.requests] == ["2", "1"]


async def test_run_insights_empty() -> None:
    result = await make(FakeMeta([])).run_insights({"fields": ["spend", "impressions"]})
    assert result.headers == ["spend", "impressions"]
    assert result.rows == []


def test_spec_validation() -> None:
    with pytest.raises(ValueError):
        InsightsSpec(fields=[])
    with pytest.raises(ValueError, match="together"):
        InsightsSpec(fields=["spend"], since="2026-01-01")
    with pytest.raises(ValueError, match="not both"):
        InsightsSpec(
            fields=["spend"], date_preset="last_7d", since="2026-01-01", until="2026-01-31"
        )


async def test_reports_are_csv_files() -> None:
    connector = make(FakeMeta(ROWS))
    items = [i async for i in connector.list()]

    assert [i.name for i in items] == ["spend.csv"]
    assert items[0].type is ItemType.FILE
    assert items[0].mime_type == "text/csv"
    assert items[0].metadata["ad_account"] == "act_123"
    assert await connector.get_metadata("spend") == items[0]
    with pytest.raises(NotFoundError):
        await connector.get_metadata("nope")
    with pytest.raises(NotFoundError):
        [i async for i in connector.list("sub")]


async def test_download_streams_csv_page_by_page() -> None:
    plain = [{"campaign_name": r["campaign_name"], "spend": r["spend"]} for r in ROWS]
    connector = make(FakeMeta(plain), page_size=2)
    item = await connector.get_metadata("spend")

    chunks = [c async for c in connector.download(item)]

    assert len(chunks) == 2
    assert b"".join(chunks).decode() == (
        'campaign_name,spend\r\nSpring,10.50\r\n"Summer, EU",7.00\r\nAutumn,1.25\r\n'
    )


async def test_download_runs_the_configured_report_not_the_item() -> None:
    fake = FakeMeta(ROWS)
    connector = make(fake)
    item = await connector.get_metadata("spend")
    forged = item.model_copy(update={"metadata": {"fields": ["secret"]}})

    [c async for c in connector.download(forged)]

    assert fake.requests[0].url.params["fields"] == "campaign_name,spend"
    with pytest.raises(NotFoundError):
        [c async for c in connector.download(item.model_copy(update={"id": "other"}))]


async def test_app_secret_adds_appsecret_proof() -> None:
    fake = FakeMeta([])
    client = client_for(fake, app_secret="s3cret")
    await client.get("act_123", {"fields": "id"})

    expected = hmac.new(b"s3cret", TOKEN.encode(), hashlib.sha256).hexdigest()
    assert fake.requests[0].url.params["appsecret_proof"] == expected
    assert TOKEN not in str(fake.requests[0].url)  # the token travels in a header


def raw_client(handler: Any) -> MetaAdsClient:
    return MetaAdsClient(TOKEN, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_error_mapping() -> None:
    cases = [
        (graph_error(190, "expired"), AuthenticationError),
        (graph_error(200, "no permission", 403), AuthenticationError),
        (graph_error(803, "gone"), NotFoundError),
        (graph_error(100, "bad field"), DataSourceError),
    ]
    for response, error in cases:
        with pytest.raises(error):
            await raw_client(lambda r, resp=response: resp).get("act_123")


async def test_throttling_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("data_sources.connectors.meta_ads.client.asyncio.sleep", fake_sleep)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return graph_error(17, "slow down") if calls < 3 else httpx.Response(200, json={"ok": 1})

    assert await raw_client(handler).get("act_123") == {"ok": 1}
    assert calls == 3 and len(slept) == 2

    with pytest.raises(RateLimitError):
        await raw_client(lambda r: graph_error(80004)).get("act_123", max_retries=2)


async def test_long_account_block_raises_instead_of_sleeping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_: float) -> None:
        raise AssertionError("must not sleep through a long block")

    monkeypatch.setattr("data_sources.connectors.meta_ads.client.asyncio.sleep", no_sleep)
    usage = json.dumps({"123": [{"estimated_time_to_regain_access": 30}]})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": 80004, "message": "too many calls"}},
            headers={"x-business-use-case-usage": usage},
        )

    with pytest.raises(RateLimitError, match="30 minutes"):
        await raw_client(handler).get("act_123/insights")


def test_authorize_url() -> None:
    url = authorize_url("app1", "http://localhost/cb", "st4te")
    assert url.startswith("https://www.facebook.com/v26.0/dialog/oauth?")
    assert "client_id=app1" in url and "state=st4te" in url and "scope=ads_read" in url


async def test_token_exchange() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        assert request.url.path == "/v26.0/oauth/access_token"
        if request.url.params.get("code") == "bad":
            return graph_error(100, "code expired")
        if "fb_exchange_token" in request.url.params:
            return httpx.Response(200, json={"access_token": "long", "expires_in": 5183944})
        return httpx.Response(200, json={"access_token": "short"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        short = await exchange_code(http, "app", "sec", "http://cb", "c0de")
        long = await extend_token(http, "app", "sec", short.token)
        with pytest.raises(AuthenticationError, match="code expired"):
            await exchange_code(http, "app", "sec", "http://cb", "bad")

    assert (short.token, short.expires_in) == ("short", None)
    assert (long.token, long.expires_in) == ("long", 5183944)
    assert seen[1]["fb_exchange_token"] == "short" and seen[1]["grant_type"] == "fb_exchange_token"
