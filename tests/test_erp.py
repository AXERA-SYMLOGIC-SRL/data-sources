"""ERP sources: drop-in discovery, the catalog, and the Odoo adapter on a fake Odoo (JSON-2 for 19,
JSON-RPC `execute_kw` before)."""

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
    CATALOG,
    ENTITIES,
    Cursor,
    ErpSource,
    create_erp_source,
    discover,
    erp_sources,
)
from data_sources.erp.registry import ErpSourceRegistry
from data_sources.erp.sources.odoo import ODOO, OdooSource
from data_sources.reports import ReportError

KEY = "private-api-key-123"
UTC = dt.UTC

# --- the catalog ---------------------------------------------------------------------------------


def test_catalog_references_resolve_and_names_are_unique() -> None:
    for entity in CATALOG:
        names = [c.name for c in entity.columns]
        assert len(names) == len(set(names)), entity.name
        assert not {"id", "updated_at", "raw"} & set(names), entity.name
        for column in entity.columns:
            assert column.type != "ref" or column.references in ENTITIES, (entity.name, column.name)


def test_ref_columns_have_a_name_column() -> None:
    order = ENTITIES["sale_orders"]
    assert order.column("partner_id").name_column == "partner_name"
    assert order.column("number").name_column is None
    assert order.references["partner_id"] == "partners"


def test_odoo_maps_every_catalog_entity_onto_catalog_columns() -> None:
    assert set(ODOO) == set(ENTITIES)
    for name, spec in ODOO.items():
        assert set(spec.columns) <= {c.name for c in ENTITIES[name].columns}, name


# --- discovery -----------------------------------------------------------------------------------

PLUGIN = """
from data_sources.erp.base import ErpSource, Page, SourceInfo


class {name}(ErpSource):
    provider = "{provider}"

    async def check(self):
        return SourceInfo("1", "rest", "me", [])

    async def entities(self):
        return ["partners"]

    async def describe(self, entity):
        return []

    async def count(self, entity):
        return 0

    async def read(self, entity, cursor=None, page_size=1000):
        return Page([], None)

    async def list_ids(self, entity):
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
        "parent_id": False,
        "customer_rank": 1,
        "supplier_rank": 0,
        "email": False,
        "phone": "123",
        "city": "Iasi",
        "country_id": [185, "Romania"],
        "vat": False,
        "user_id": [2, "Mitchell Admin"],
        "active": True,
        "create_date": "2026-01-02 03:04:05",
        "company_id": [1, "My Company"],
        "write_date": written,
        "access_token": "portal-secret",
    }


class FakeOdoo:
    """Odoo 19 (`json2`) or 18 (`jsonrpc`), with `records` per model, filtered by `write_date >=`
    and paged by offset/limit like the real one."""

    FIELDS = {
        "name": {"type": "char", "store": True, "string": "Name"},
        "x_studio_tier": {"type": "selection", "store": True, "string": "Tier"},
        "access_token": {"type": "char", "store": True, "string": "Security Token"},
        "child_ids": {"type": "one2many", "store": True, "string": "Contacts"},
        "message_ids": {"type": "one2many", "store": True, "string": "Messages"},
        "image_1920": {"type": "binary", "store": True, "string": "Image"},
        "comment": {"type": "html", "store": True, "string": "Notes"},
        "display_name": {"type": "char", "store": False, "string": "Display name"},
        "country_id": {
            "type": "many2one",
            "store": True,
            "relation": "res.country",
            "string": "Country",
        },
        "write_date": {"type": "datetime", "store": True, "string": "Last updated"},
        # Every other field the partner mapping reads.
        **{
            n: {"type": "char", "store": True, "string": n}
            for n in ODOO["partners"].native
            if n != "country_id"
        },
    }

    def __init__(
        self, version: int = 19, records: dict[str, list[dict[str, Any]]] | None = None
    ) -> None:
        self.version = version
        self.records = records or {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == "/web/webclient/version_info":
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
            return self.answer(model, method, body)
        if path == "/jsonrpc":
            params = body["params"]
            if params["service"] == "common":
                db, login, key, _ = params["args"]
                ok = db == "shop" and login == "admin" and key == KEY
                return httpx.Response(200, json={"jsonrpc": "2.0", "result": 2 if ok else False})
            db, uid, key, model, method, _args, kwargs = params["args"]
            if key != KEY:
                error = {"code": 200, "data": {"name": "odoo.exceptions.AccessDenied"}}
                return httpx.Response(200, json={"jsonrpc": "2.0", "error": error})
            response = self.answer(model, method, kwargs)
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": response.json()})
        return httpx.Response(404, text="nope")

    def answer(self, model: str, method: str, kwargs: dict[str, Any]) -> httpx.Response:
        self.calls.append((model, method, kwargs))
        if method == "context_get":
            return httpx.Response(200, json={"uid": 2, "lang": "en_US"})
        if model == "res.users" and method == "search_read":
            return httpx.Response(200, json=[{"id": 2, "name": "Mitchell Admin", "login": "admin"}])
        if model == "res.users" and method == "search_count":
            return httpx.Response(200, json=1)
        if model == "res.company" and method == "search_read":
            return httpx.Response(
                200, json=[{"id": 1, "name": "My Company", "currency_id": [2, "RON"]}]
            )
        if model == "ir.model":
            return httpx.Response(200, json=[{"model": m} for m in ("res.partner", "sale.order")])
        if method == "fields_get":
            return httpx.Response(200, json=self.FIELDS)
        rows = self.records.get(model)
        if rows is None:
            return httpx.Response(
                404, json={"message": f"the model '{model}' does not exist", "debug": "tb"}
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
        async def no_wait(_: float) -> None:
            return None

        source = create_erp_source(
            odoo_config(**credentials), transport=httpx.MockTransport(self.handler), sleep=no_wait
        )
        assert isinstance(source, OdooSource)
        return source


# --- Odoo ----------------------------------------------------------------------------------------


async def test_check_reports_version_user_and_companies() -> None:
    info = await FakeOdoo().source().check()
    assert (info.version, info.api, info.user) == ("19.0", "json2", "admin")
    assert [(c.id, c.name, c.currency) for c in info.companies] == [("1", "My Company", "RON")]


async def test_reads_include_archived_records_and_every_company() -> None:
    fake = FakeOdoo(records={"res.partner": [partner(1, "2026-10-01 10:00:00")]})
    await fake.source().read("partners")
    model, method, kwargs = fake.calls[-1]
    assert (model, method) == ("res.partner", "search_read")
    assert kwargs["context"] == {"active_test": False, "allowed_company_ids": [1]}
    assert kwargs["order"] == "write_date asc, id asc"


async def test_records_have_core_columns_names_and_the_raw_record_without_secrets() -> None:
    fake = FakeOdoo(records={"res.partner": [partner(7, "2026-10-01 10:00:00")]})
    [record] = (await fake.source().read("partners")).records
    assert record.id == "7" and record.updated_at == dt.datetime(2026, 10, 1, 10, tzinfo=UTC)
    values = record.values
    assert (
        values["name"] == "Partner 7"
        and values["is_customer"] is True
        and values["is_supplier"] is False
    )
    assert values["country"] == "Romania" and values["email"] is None
    assert (values["salesperson_id"], values["salesperson_name"]) == ("2", "Mitchell Admin")
    assert values["company_id"] == "1" and values["created_at"] == dt.datetime(
        2026, 1, 2, 3, 4, 5, tzinfo=UTC
    )
    read_fields = fake.calls[-1][2]["fields"]
    assert "x_studio_tier" in read_fields and "name" in read_fields
    for skipped in ("access_token", "child_ids", "message_ids", "image_1920", "comment"):
        assert skipped not in read_fields
    assert "access_token" not in record.raw


async def test_paging_survives_many_records_changed_in_the_same_second() -> None:
    rows = [partner(i, "2026-10-01 10:00:00") for i in range(1, 8)] + [
        partner(8, "2026-10-01 10:00:05")
    ]
    source = FakeOdoo(records={"res.partner": rows}).source()
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        page = await source.read("partners", cursor, page_size=3)
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
        .read("partners", Cursor(dt.datetime(2026, 10, 2, tzinfo=UTC)))
    )
    assert [r.id for r in page.records] == ["2"] and page.next_cursor is None


async def test_count_ids_and_installed_entities() -> None:
    rows = [partner(1, "2026-10-01 10:00:00"), partner(2, "2026-10-02 10:00:00")]
    source = FakeOdoo(records={"res.partner": rows}).source()
    assert await source.count("partners") == 2
    assert await source.list_ids("partners") == ["1", "2"]
    assert await source.entities() == ["partners", "sale_orders"]
    fields = {f.name: f for f in await source.describe("partners")}
    assert (
        fields["x_studio_tier"].type == "selection"
        and fields["country_id"].relation == "res.country"
    )


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

    source = create_erp_source(
        odoo_config(), transport=httpx.MockTransport(handler), sleep=lambda _: _noop()
    )
    with pytest.raises(ReportError) as caught:
        await source.check()
    assert caught.value.reason == "unreachable"


async def _noop() -> None:
    return None


async def test_an_unknown_model_is_refused_without_odoo_s_traceback() -> None:
    with pytest.raises(ReportError) as caught:
        await FakeOdoo(records={}).source().read("partners")
    assert caught.value.reason == "request_failed" and "tb" not in str(caught.value)


async def test_not_configured() -> None:
    with pytest.raises(ReportError) as caught:
        await FakeOdoo().source(url="").check()
    assert caught.value.reason == "not_configured"


async def test_odoo_before_19_reads_over_jsonrpc_with_the_login() -> None:
    fake = FakeOdoo(version=18, records={"res.partner": [partner(3, "2026-10-01 10:00:00")]})
    source = fake.source(username="admin")
    info = await source.check()
    assert (info.api, info.user) == ("jsonrpc", "admin")
    [record] = (await source.read("partners")).records
    assert record.values["name"] == "Partner 3"


async def test_odoo_before_19_needs_the_login() -> None:
    with pytest.raises(ReportError) as caught:
        await FakeOdoo(version=18).source().check()
    assert caught.value.reason == "request_failed" and "login" in caught.value.message


async def test_odoo_before_19_refused_key_is_auth_failed() -> None:
    with pytest.raises(ReportError) as caught:
        await FakeOdoo(version=18).source(username="admin", api_key="wrong").check()
    assert caught.value.reason == "auth_failed"
