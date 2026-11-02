"""ERP sources: drop-in discovery, and the Odoo source on a fake Odoo (JSON-2 for 19 and later,
JSON-RPC `execute_kw` before, and as the fallback for a password)."""

from __future__ import annotations

import datetime as dt
import json
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from data_sources.config.schema import AuthConfig, ConnectorConfig
from data_sources.core.exceptions import ConnectorNotFoundError
from data_sources.erp import (
    Cursor,
    ErpSource,
    create_erp_source,
    discover,
    erp_sources,
)
from data_sources.erp.registry import ErpSourceRegistry
from data_sources.erp.sources.odoo import OdooSource, classify
from data_sources.reports import ReportError

KEY = "private-api-key-123"
PASSWORD = "admin-password"
UTC = dt.UTC
PARTNERS = {"model": "res.partner"}

# --- discovery -----------------------------------------------------------------------------------

PLUGIN = """
from data_sources.erp.base import ErpSource, Page, SourceInfo


class {name}(ErpSource):
    provider = "{provider}"

    async def check(self):
        return SourceInfo("1", "rest", "me", [])

    async def available(self, specs):
        return list(specs)

    async def fields(self, spec):
        return []

    async def count(self, spec):
        return 0

    async def read(self, spec, cursor=None, page_size=1000):
        return Page([], None)

    async def list_ids(self, spec):
        return []
"""


@pytest.fixture
def plugins(tmp_path: Path) -> Iterator[Path]:
    name = f"erp_plugins_{tmp_path.name}"
    package = tmp_path / name
    package.mkdir()
    (package / "__init__.py").write_text("")
    sys.path.insert(0, str(tmp_path))
    yield package
    sys.path.remove(str(tmp_path))
    for module in [m for m in sys.modules if m == name or m.startswith(f"{name}.")]:
        del sys.modules[module]


def test_odoo_is_discovered_and_built_from_config() -> None:
    assert "odoo" in erp_sources.providers()
    assert "odoo" in discover()
    source = create_erp_source(odoo_config())
    assert isinstance(source, OdooSource) and source.configured


def test_a_dropped_in_file_is_a_new_erp(plugins: Path) -> None:
    (plugins / "dolibarr.py").write_text(
        textwrap.dedent(PLUGIN.format(name="Dolibarr", provider="dolibarr"))
    )
    registry = ErpSourceRegistry(plugins.name)
    assert registry.providers() == ["dolibarr"]
    assert isinstance(registry.create(ConnectorConfig(provider="dolibarr")), ErpSource)


def test_two_erps_for_one_provider_fail_loudly(plugins: Path) -> None:
    for module in ("one", "two"):
        (plugins / f"{module}.py").write_text(
            textwrap.dedent(PLUGIN.format(name=module.title(), provider="x"))
        )
    with pytest.raises(RuntimeError, match="Two ERP sources claim 'x'"):
        discover(plugins.name)


def test_unknown_erp() -> None:
    with pytest.raises(ConnectorNotFoundError):
        create_erp_source(ConnectorConfig(provider="sap"))


def test_configured_needs_url_database_and_key() -> None:
    assert not create_erp_source(odoo_config(api_key="")).configured


# --- a fake Odoo ---------------------------------------------------------------------------------


def odoo_config(**credentials: str) -> ConnectorConfig:
    base = {"url": "http://odoo.test", "database": "shop", "api_key": KEY}
    return ConnectorConfig(
        provider="odoo", auth=AuthConfig(type="api_key", credentials={**base, **credentials})
    )


def partner(i: int, written: str) -> dict[str, Any]:
    return {
        "id": i,
        "name": f"Partner {i}",
        "is_company": True,
        "country_id": [185, "Romania"],
        "email": False,
        "x_studio_tier": "gold",
        "display_name": f"Partner {i}",
        "write_date": written,
        "access_token": "portal-secret",
        "child_ids": [9],
        "image_1920": "base64",
    }


async def no_wait(_: float) -> None:
    return None


class FakeOdoo:
    """Odoo `version` (JSON-2 from 19, JSON-RPC always), with `records` per model, filtered by
    `write_date >=` and paged by offset/limit like the real one. JSON-2 takes only the key;
    JSON-RPC takes the key or the password. Models in `forbidden` answer `AccessError`."""

    FIELDS = {
        "name": {"type": "char", "store": True, "string": "Name"},
        "is_company": {"type": "boolean", "store": True, "string": "Is a Company"},
        "email": {"type": "char", "store": True, "string": "Email"},
        "x_studio_tier": {
            "type": "selection",
            "store": True,
            "string": "Tier",
            "selection": [["gold", "Gold"], ["silver", "Silver"]],
        },
        "access_token": {"type": "char", "store": True, "string": "Security Token"},
        "child_ids": {"type": "one2many", "store": True, "string": "Contacts"},
        "message_ids": {"type": "one2many", "store": True, "string": "Messages"},
        "message_main_attachment_id": {
            "type": "many2one",
            "store": True,
            "relation": "ir.attachment",
            "string": "Main Attachment",
        },
        "message_type": {"type": "selection", "store": True, "string": "Type"},
        # A field named like JSON-RPC's error envelope (`l10n.ro.message.spv.error`).
        "error": {"type": "text", "store": True, "string": "Error"},
        # Credentials SECRET_WORDS don't name; SEO keywords and a flag are no credentials.
        "key_download": {"type": "char", "store": True, "string": "Key Download"},
        "attendance_kiosk_key": {"type": "char", "store": True, "string": "Kiosk Key"},
        "l10n_ro_edi_client_id": {"type": "char", "store": True, "string": "Client ID"},
        "proxy_ip": {"type": "char", "store": True, "string": "Proxy IP"},
        "website_meta_keywords": {"type": "char", "store": True, "string": "SEO Keywords"},
        "hr_presence_control_ip": {"type": "boolean", "store": True, "string": "Based on IP"},
        "image_1920": {"type": "binary", "store": True, "string": "Image"},
        "comment": {"type": "html", "store": True, "string": "Notes"},
        "display_name": {"type": "char", "store": False, "string": "Display name"},
        "country_id": {
            "type": "many2one",
            "store": True,
            "relation": "res.country",
            "string": "Country",
            "help": "The country.",
        },
        "write_date": {"type": "datetime", "store": True, "string": "Last updated"},
    }

    #: `ir.model`: what the database holds (models it lists without a stored `write_date`:
    #: `l10n.ro.no_log`).
    CATALOGUE = [
        {"model": "sale.order", "name": "Sales Order", "modules": "sale"},
        {"model": "l10n.ro.message.spv", "name": "SPV Message", "modules": "l10n_ro_message_spv"},
        {"model": "l10n.ro.no_log", "name": "No log", "modules": "l10n_ro_x"},
        {"model": "ir.cron", "name": "Scheduled Actions", "modules": "base"},
        {"model": "mail.message", "name": "Message", "modules": "mail"},
        {"model": "res.users.settings", "name": "User Settings", "modules": "base, mail"},
        {"model": "website.page", "name": "Page", "modules": "website"},
        {"model": "x_fleet_tyre", "name": "Tyres", "modules": ""},
    ]

    def __init__(
        self,
        version: int = 19,
        records: dict[str, list[dict[str, Any]]] | None = None,
        forbidden: tuple[str, ...] = (),
    ) -> None:
        self.version = version
        self.records = records or {}
        self.forbidden = forbidden
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.versions_asked = 0
        self.model_domains: list[Any] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == "/web/webclient/version_info":
            self.versions_asked += 1
            info = {
                "server_version": f"{self.version}.0",
                "server_version_info": [self.version, 0, 0],
            }
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": info})
        if path.startswith("/json/2/"):
            if self.version < 19:
                return httpx.Response(404, text="<html>Not Found</html>")
            if request.headers.get("Authorization") != f"bearer {KEY}":
                return httpx.Response(
                    401, json={"name": "Unauthorized", "message": "Invalid apikey", "debug": KEY}
                )
            if request.headers.get("X-Odoo-Database") != "shop":
                return httpx.Response(404, text="<!DOCTYPE html><h1>Not Found</h1>")
            model, method = path.split("/")[3:5]
            if model in self.forbidden:
                error: dict[str, Any] = {
                    "name": "odoo.exceptions.AccessError",
                    "message": "No access",
                    "debug": "tb",
                }
                return httpx.Response(403, json=error)
            return self.answer(model, method, body)
        if path == "/jsonrpc":
            params = body["params"]
            if params["service"] == "common":
                db, login, key, _ = params["args"]
                ok = db == "shop" and login == "admin" and key in (KEY, PASSWORD)
                return httpx.Response(200, json={"jsonrpc": "2.0", "result": 2 if ok else False})
            db, uid, key, model, method, _args, kwargs = params["args"]
            if key not in (KEY, PASSWORD):
                error = {"code": 200, "data": {"name": "odoo.exceptions.AccessDenied"}}
                return httpx.Response(200, json={"jsonrpc": "2.0", "error": error})
            if model in self.forbidden:
                data = {"name": "odoo.exceptions.AccessError", "message": "No", "debug": "tb"}
                return httpx.Response(200, json={"jsonrpc": "2.0", "error": {"data": data}})
            response = self.answer(model, method, kwargs)
            if response.is_error:
                data = {"name": "builtins.KeyError", "message": model, "debug": "tb"}
                return httpx.Response(200, json={"jsonrpc": "2.0", "error": {"data": data}})
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": response.json()})
        return httpx.Response(404, text="nope")

    def answer(self, model: str, method: str, kwargs: dict[str, Any]) -> httpx.Response:
        self.calls.append((model, method, kwargs))
        if method == "context_get":
            return httpx.Response(200, json={"uid": 2, "lang": "en_US"})
        if model == "res.users" and method == "search_read":
            return httpx.Response(200, json=[{"id": 2, "name": "Mitchell Admin", "login": "admin"}])
        if model == "res.company" and method == "search_read":
            return httpx.Response(
                200,
                json=[
                    {"id": 1, "name": "My Company", "currency_id": [2, "RON"]},
                    {"id": 3, "name": "Other", "currency_id": [1, "EUR"]},
                ],
            )
        if model == "ir.model" and method == "fields_get":
            meta = {"model": {"type": "char"}, "transient": {"type": "boolean"}}
            return httpx.Response(
                200, json={**meta, "abstract": {"type": "boolean"}} if self.version >= 19 else meta
            )
        if model == "ir.model" and kwargs["domain"][0][0] == "transient":
            self.model_domains.append(kwargs["domain"])
            return httpx.Response(200, json=self.CATALOGUE)
        if model == "ir.model.fields":
            stamped = [m["model"] for m in self.CATALOGUE if m["model"] != "l10n.ro.no_log"]
            return httpx.Response(200, json=[{"model": m} for m in stamped])
        if model == "ir.model":
            installed = ("res.partner", "sale.order", "account.move", *self.forbidden)
            wanted = kwargs["domain"][0][2]
            return httpx.Response(200, json=[{"model": m} for m in installed if m in wanted])
        if method == "fields_get":
            return httpx.Response(200, json=self.FIELDS)
        if method == "formatted_read_group":
            groups = [{"country_id": [185, "Romania"], "id:count_distinct": 2, "__count": 2}]
            return httpx.Response(200, json=groups)
        if method == "read_group":
            groups = [
                {
                    "country_id": [185, "Romania"],
                    "id": 2,
                    "__count": 2,
                    "__domain": [["country_id", "=", 185]],
                    "__context": {},
                }
            ]
            return httpx.Response(200, json=groups)
        rows = self.records.get(model)
        if rows is None:
            if method == "search_count" and model in ("sale.order", "account.move"):
                return httpx.Response(200, json=0)
            return httpx.Response(
                404,
                json={
                    "name": "werkzeug.exceptions.NotFound",
                    "message": f"the model '{model}' does not exist",
                    "debug": "tb",
                },
            )
        since = next((t[2] for t in kwargs.get("domain", []) if t[0] == "write_date"), None)
        found = sorted(
            (r for r in rows if since is None or r["write_date"] >= since),
            key=lambda r: (r["write_date"], r["id"]),
        )
        if method == "search_count":
            return httpx.Response(200, json=len(found))
        if method == "search":
            return httpx.Response(200, json=[r["id"] for r in found])
        offset, limit = kwargs.get("offset", 0), kwargs.get("limit") or len(found)
        page = found[offset : offset + limit]
        return httpx.Response(
            200,
            json=[{k: v for k, v in r.items() if k in kwargs["fields"] or k == "id"} for r in page],
        )

    def source(self, **credentials: str) -> OdooSource:
        source = create_erp_source(
            odoo_config(**credentials), transport=httpx.MockTransport(self.handler), sleep=no_wait
        )
        assert isinstance(source, OdooSource)
        return source


# --- Odoo ----------------------------------------------------------------------------------------


async def test_check_reports_version_user_and_companies() -> None:
    info = await FakeOdoo().source().check()
    assert (info.version, info.api, info.user) == ("19.0", "json2", "admin")
    assert [(c.id, c.name, c.currency) for c in info.companies] == [
        ("1", "My Company", "RON"),
        ("3", "Other", "EUR"),
    ]


async def test_reads_include_archived_records_every_company_in_english() -> None:
    fake = FakeOdoo(records={"res.partner": [partner(1, "2026-10-01 10:00:00")]})
    await fake.source().read(PARTNERS)
    model, method, kwargs = fake.calls[-1]
    assert (model, method) == ("res.partner", "search_read")
    assert kwargs["context"] == {
        "active_test": False,
        "lang": "en_US",
        "allowed_company_ids": [1, 3],
    }
    assert kwargs["order"] == "write_date asc, id asc"


async def test_records_come_back_as_odoo_answers_without_secrets_or_bulky_fields() -> None:
    fake = FakeOdoo(records={"res.partner": [partner(7, "2026-10-01 10:00:00")]})
    [record] = (await fake.source().read(PARTNERS)).records
    assert record.id == "7" and record.updated_at == dt.datetime(2026, 10, 1, 10, tzinfo=UTC)
    assert record.raw["country_id"] == [185, "Romania"] and record.raw["email"] is False
    assert record.raw["x_studio_tier"] == "gold"
    read_fields = fake.calls[-1][2]["fields"]
    assert {"name", "x_studio_tier", "country_id", "write_date"} <= set(read_fields)
    for skipped in ("access_token", "child_ids", "message_ids", "image_1920", "comment"):
        assert skipped not in read_fields and skipped not in record.raw
    # Not stored and not asked for as `extra`.
    assert "display_name" not in read_fields


async def test_extra_fields_are_read_only_when_the_model_has_them() -> None:
    fake = FakeOdoo(records={"res.partner": [partner(7, "2026-10-01 10:00:00")]})
    await fake.source().read({**PARTNERS, "extra": ["display_name", "list_price"]})
    read_fields = fake.calls[-1][2]["fields"]
    assert "display_name" in read_fields and "list_price" not in read_fields


async def test_explicit_fields_replace_the_stored_ones() -> None:
    fake = FakeOdoo(records={"res.partner": [partner(7, "2026-10-01 10:00:00")]})
    await fake.source().read({**PARTNERS, "fields": ["name", "access_token", "nope"]})
    # Asked for by name, so read (the application decided); unknown names are dropped.
    assert fake.calls[-1][2]["fields"] == ["access_token", "name", "write_date"]


async def test_a_spec_domain_is_kept_and_the_cursor_added() -> None:
    fake = FakeOdoo(records={"res.partner": [partner(1, "2026-10-01 10:00:00")]})
    spec = {**PARTNERS, "domain": [("is_company", "=", True)]}
    await fake.source().read(spec, Cursor(dt.datetime(2026, 9, 1, 12, 30, tzinfo=UTC)))
    assert fake.calls[-1][2]["domain"] == [
        ["is_company", "=", True],
        ["write_date", ">=", "2026-09-01 12:30:00"],
    ]


async def test_paging_survives_many_records_changed_in_the_same_second() -> None:
    rows = [partner(i, "2026-10-01 10:00:00") for i in range(1, 8)] + [
        partner(8, "2026-10-01 10:00:05")
    ]
    source = FakeOdoo(records={"res.partner": rows}).source()
    seen: list[str] = []
    cursor = None
    async with source:
        for _ in range(10):
            page = await source.read(PARTNERS, cursor, page_size=3)
            seen += [r.id for r in page.records]
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
    assert seen == [str(i) for i in range(1, 9)]


async def test_an_incremental_read_starts_at_the_cursor_time() -> None:
    rows = [partner(1, "2026-10-01 10:00:00"), partner(2, "2026-10-02 10:00:00")]
    page = (
        await FakeOdoo(records={"res.partner": rows})
        .source()
        .read(PARTNERS, Cursor(dt.datetime(2026, 10, 2, tzinfo=UTC)))
    )
    assert [r.id for r in page.records] == ["2"] and page.next_cursor is None


async def test_one_entered_source_signs_in_once() -> None:
    rows = [partner(1, "2026-10-01 10:00:00")]
    fake = FakeOdoo(records={"res.partner": rows})
    source = fake.source()
    async with source:
        await source.check()
        await source.count(PARTNERS)
        await source.read(PARTNERS)
        await source.list_ids(PARTNERS)
    assert fake.versions_asked == 1
    assert [c[1] for c in fake.calls].count("context_get") == 1


async def test_count_ids_fields_and_available_models() -> None:
    rows = [partner(1, "2026-10-01 10:00:00"), partner(2, "2026-10-02 10:00:00")]
    source = FakeOdoo(records={"res.partner": rows}, forbidden=("hr.employee",)).source()
    assert await source.count(PARTNERS) == 2
    assert await source.list_ids(PARTNERS) == ["1", "2"]
    specs = {
        "res_partner": PARTNERS,
        "sale_order": {"model": "sale.order"},
        "mrp_production": {"model": "mrp.production"},  # not installed
        "hr_employee": {"model": "hr.employee"},  # installed, not readable
    }
    assert await source.available(specs) == ["res_partner", "sale_order"]
    fields = {f.name: f for f in await source.fields(PARTNERS)}
    assert fields["x_studio_tier"].selection == (("gold", "Gold"), ("silver", "Silver"))
    assert fields["country_id"].relation == "res.country" and fields["country_id"].help
    assert fields["display_name"].stored is False


async def test_fields_say_whether_a_plain_read_takes_them() -> None:
    fields = {f.name: f for f in await FakeOdoo().source().fields(PARTNERS)}
    assert fields["name"].readable and fields["x_studio_tier"].readable
    # Files, HTML, child lists, the chatter, credentials and computed fields aren't read.
    for name in (
        "image_1920",
        "comment",
        "child_ids",
        "message_ids",
        "message_main_attachment_id",
        "access_token",
        "display_name",
        "key_download",
        "attendance_kiosk_key",
        "l10n_ro_edi_client_id",
        "proxy_ip",
    ):
        assert not fields[name].readable, name
    # A module's own plain field under a chatter-like name is read (the kind of an ANAF message),
    # and a field named `error` is a field, not Odoo refusing (JSON-2 answers with the result).
    assert fields["message_type"].readable and fields["error"].readable
    assert fields["website_meta_keywords"].readable and fields["hr_presence_control_ip"].readable


@pytest.mark.parametrize("version", [19, 17])
async def test_models_lists_business_records_of_modules_and_studio(version: int) -> None:
    odoo = FakeOdoo(version=version)
    found = await odoo.source(username="admin").models()
    assert [(m.name, m.label, m.modules) for m in found] == [
        ("l10n.ro.message.spv", "SPV Message", ("l10n_ro_message_spv",)),
        ("sale.order", "Sales Order", ("sale",)),
        ("x_fleet_tyre", "Tyres", ()),
    ]
    # Wizards always left out; abstract models where Odoo can say so (19 and later).
    expected = [["transient", "=", False]] + ([["abstract", "=", False]] if version >= 19 else [])
    assert odoo.model_domains == [expected]


async def test_a_model_the_user_may_not_read_is_access_denied() -> None:
    for version, credentials in ((19, {}), (18, {"username": "admin"})):
        source = FakeOdoo(version=version, forbidden=("hr.employee",)).source(**credentials)
        with pytest.raises(ReportError) as caught:
            await source.read({"model": "hr.employee"})
        assert caught.value.reason == "access_denied" and not caught.value.auth
        assert "tb" not in str(caught.value)


@pytest.mark.parametrize(
    ("credentials", "reason"),
    [
        ({"api_key": "wrong"}, "auth_failed"),
        ({"database": "other"}, "database_not_found"),
    ],
)
async def test_connection_errors(credentials: dict[str, str], reason: str) -> None:
    with pytest.raises(ReportError) as caught:
        await FakeOdoo().source(**credentials).check()
    assert (
        caught.value.reason == reason
        and KEY not in str(caught.value)
        and "debug" not in str(caught.value)
    )


async def test_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    source = create_erp_source(odoo_config(), transport=httpx.MockTransport(handler), sleep=no_wait)
    with pytest.raises(ReportError) as caught:
        await source.check()
    assert caught.value.reason == "unreachable"


async def test_an_unknown_model_is_refused_without_odoo_s_traceback() -> None:
    with pytest.raises(ReportError) as caught:
        await FakeOdoo(records={}).source().read({"model": "x.nope"})
    assert caught.value.reason == "request_failed" and "tb" not in str(caught.value)
    assert "does not exist" in caught.value.diagnostics


async def test_a_spec_without_a_model_is_refused() -> None:
    with pytest.raises(ReportError) as caught:
        await FakeOdoo().source().read({})
    assert caught.value.reason == "request_failed"


async def test_not_configured() -> None:
    with pytest.raises(ReportError) as caught:
        await FakeOdoo().source(url="").check()
    assert caught.value.reason == "not_configured"


async def test_odoo_before_19_reads_over_jsonrpc_with_the_login() -> None:
    fake = FakeOdoo(version=18, records={"res.partner": [partner(3, "2026-10-01 10:00:00")]})
    source = fake.source(username="admin")
    info = await source.check()
    assert (info.api, info.user) == ("jsonrpc", "admin")
    [record] = (await source.read(PARTNERS)).records
    assert record.raw["name"] == "Partner 3"


async def test_odoo_before_19_needs_the_login() -> None:
    with pytest.raises(ReportError) as caught:
        await FakeOdoo(version=18).source().check()
    assert caught.value.reason == "request_failed" and "login" in caught.value.message


async def test_odoo_before_19_refused_key_is_auth_failed() -> None:
    with pytest.raises(ReportError) as caught:
        await FakeOdoo(version=18).source(username="admin", api_key="wrong").check()
    assert caught.value.reason == "auth_failed"


async def test_a_password_falls_back_to_jsonrpc_on_19_with_a_login() -> None:
    fake = FakeOdoo(records={"res.partner": [partner(3, "2026-10-01 10:00:00")]})
    source = fake.source(username="admin", api_key=PASSWORD)
    info = await source.check()
    assert (info.version, info.api) == ("19.0", "jsonrpc")
    assert [r.id for r in (await source.read(PARTNERS)).records] == ["3"]


async def test_a_refused_key_without_a_login_stays_refused_on_19() -> None:
    with pytest.raises(ReportError) as caught:
        await FakeOdoo().source(api_key=PASSWORD).check()
    assert caught.value.reason == "auth_failed" and caught.value.auth


def test_a_busy_odoo_is_retried() -> None:
    request = httpx.Request("POST", "http://odoo.test/json/2/res.partner/search_read")
    for status in (429, 502, 503):
        assert type(classify(httpx.Response(status, request=request))).__name__ == "Retry"


async def test_busy_then_answering_is_read() -> None:
    fake = FakeOdoo(records={"res.partner": [partner(1, "2026-10-01 10:00:00")]})
    answers = iter([httpx.Response(503, text="busy")])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("search_read") and "res.partner" in request.url.path:
            busy = next(answers, None)
            if busy is not None:
                return busy
        return fake.handler(request)

    source = create_erp_source(odoo_config(), transport=httpx.MockTransport(handler), sleep=no_wait)
    assert len((await source.read(PARTNERS)).records) == 1


@pytest.mark.parametrize(
    ("pasted", "base"),
    [
        ("https://shop.odoo.com", "https://shop.odoo.com"),
        ("https://shop.odoo.com/", "https://shop.odoo.com"),
        ("shop.odoo.com", "https://shop.odoo.com"),
        ("https://shop.odoo.com/odoo/action-123?debug=1", "https://shop.odoo.com"),
        ("https://shop.odoo.com/web#menu_id=3&action=12", "https://shop.odoo.com"),
        ("https://shop.odoo.com/web/login", "https://shop.odoo.com"),
        ("http://localhost:8069/odoo", "http://localhost:8069"),
        ("https://erp.example.com/odoo-prod/web", "https://erp.example.com/odoo-prod"),
    ],
)
def test_a_pasted_browser_address_is_cut_to_the_instance(pasted: str, base: str) -> None:
    assert FakeOdoo().source(url=pasted).base_url == base


@pytest.mark.parametrize("version", [19, 17])
async def test_search_answers_the_records_a_domain_matches_and_their_total(version: int) -> None:
    rows = [partner(i, f"2026-10-0{i} 10:00:00") for i in range(1, 4)]
    odoo = FakeOdoo(version=version, records={"res.partner": rows})
    found, total = await odoo.source(username="admin").search(
        "res.partner", [], ["name", "country_id"], limit=2, offset=1
    )
    assert total == 3 and [r["id"] for r in found] == [2, 3]
    assert found[0] == {"id": 2, "name": "Partner 2", "country_id": [185, "Romania"]}
    call = next(c for c in odoo.calls if c[1] == "search_read" and c[0] == "res.partner")
    assert (
        call[2]["fields"] == ["name", "country_id"] and call[2]["context"]["active_test"] is False
    )


async def test_group_asks_formatted_read_group_from_19() -> None:
    odoo = FakeOdoo(version=19)
    rows = await odoo.source().group("res.partner", [], ["country_id"], ["id:count_distinct"])
    assert rows == [{"country_id": [185, "Romania"], "id:count_distinct": 2, "__count": 2}]
    call = next(c for c in odoo.calls if c[1] == "formatted_read_group")
    assert call[2]["groupby"] == ["country_id"]
    assert call[2]["aggregates"] == ["id:count_distinct", "__count"]


async def test_group_asks_read_group_before_19_in_the_same_shape() -> None:
    odoo = FakeOdoo(version=17)
    rows = await odoo.source(username="admin").group(
        "res.partner", [], ["country_id"], ["id:count_distinct"]
    )
    # The aggregate under its spec, Odoo's internal keys dropped.
    assert rows == [{"country_id": [185, "Romania"], "id:count_distinct": 2, "__count": 2}]
    call = next(c for c in odoo.calls if c[1] == "read_group")
    assert call[2]["lazy"] is False and call[2]["fields"] == ["id:count_distinct"]
