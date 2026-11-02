"""The Odoo source against a real Odoo. Opt-in:

- `ODOO_TEST_URL`, `ODOO_TEST_DB`, `ODOO_TEST_KEY` (and `ODOO_TEST_USER` for Odoo before 19, or
  to sign in with a password), e.g. the dev Odoo: `http://localhost:8069`, `odoo_demo`;
- `ODOO_TEST_DEMO=1`: a fresh database from Odoo's public demo service (`demo.odoo.com/start`,
  the newest Odoo, Enterprise, `admin`/`admin`: read through the JSON-RPC fallback).
"""

from __future__ import annotations

import datetime as dt
import os
import xml.etree.ElementTree as ET
from typing import Any

import httpx
import pytest

from data_sources.config.schema import AuthConfig, ConnectorConfig
from data_sources.erp import create_erp_source
from data_sources.erp.sources.odoo import OdooSource

MODELS = [
    "res.partner",
    "product.product",
    "sale.order",
    "sale.order.line",
    "account.move",
    "account.move.line",
    "stock.move",
    "stock.quant",
    "crm.lead",
    "project.task",
]


def _demo() -> dict[str, str]:
    body = "<?xml version='1.0'?><methodCall><methodName>start</methodName><params/></methodCall>"
    answer = httpx.post(
        "https://demo.odoo.com/start",
        content=body,
        headers={"Content-Type": "text/xml"},
        timeout=60,
    )
    found = {
        m.findtext("name"): m.findtext("value/string")
        for m in ET.fromstring(answer.text).iter("member")
    }
    return {
        "url": str(found["host"]),
        "database": str(found["database"]),
        "username": str(found["user"]),
        "api_key": str(found["password"]),
    }


def _credentials() -> dict[str, str] | None:
    if os.getenv("ODOO_TEST_DEMO") == "1":
        return _demo()
    if not os.getenv("ODOO_TEST_URL"):
        return None
    found = {
        "url": os.environ["ODOO_TEST_URL"],
        "database": os.environ["ODOO_TEST_DB"],
        "api_key": os.environ["ODOO_TEST_KEY"],
    }
    if os.getenv("ODOO_TEST_USER"):
        found["username"] = os.environ["ODOO_TEST_USER"]
    return found


pytestmark = pytest.mark.skipif(
    not (os.getenv("ODOO_TEST_URL") or os.getenv("ODOO_TEST_DEMO") == "1"),
    reason="opt-in: a real Odoo (ODOO_TEST_URL/DB/KEY, or ODOO_TEST_DEMO=1)",
)


@pytest.fixture(scope="module")
def credentials() -> dict[str, str]:
    found = _credentials()
    assert found is not None
    return found


def source(credentials: dict[str, Any]) -> OdooSource:
    built = create_erp_source(
        ConnectorConfig(provider="odoo", auth=AuthConfig(type="api_key", credentials=credentials))
    )
    assert isinstance(built, OdooSource)
    return built


async def test_reads_a_page_of_every_model_with_its_change_time(
    credentials: dict[str, str],
) -> None:
    odoo = source(credentials)
    specs = {m: {"model": m} for m in MODELS}
    async with odoo:
        info = await odoo.check()
        assert info.version and info.user and info.companies
        available = await odoo.available(specs)
        assert {"res.partner", "sale.order", "account.move.line"} <= set(available)
        for name in available:
            page = await odoo.read(specs[name], None, page_size=50)
            count = await odoo.count(specs[name])
            assert len(page.records) == min(count, 50), name
            for record in page.records:
                assert record.updated_at.tzinfo == dt.UTC
                assert "write_date" in record.raw and str(record.raw["id"]) == record.id
                assert not any("token" in key or "password" in key for key in record.raw)


async def test_paging_to_the_end_reads_every_record_once(credentials: dict[str, str]) -> None:
    odoo = source(credentials)
    spec = {"model": "res.partner"}
    seen: list[str] = []
    cursor = None
    async with odoo:
        total = await odoo.count(spec)
        while True:
            page = await odoo.read(spec, cursor, page_size=37)
            seen += [r.id for r in page.records]
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        ids = await odoo.list_ids(spec)
    assert len(seen) == len(set(seen)) == total
    assert set(seen) == set(ids)


async def test_fields_carry_selection_labels(credentials: dict[str, str]) -> None:
    fields = {f.name: f for f in await source(credentials).fields({"model": "sale.order"})}
    assert dict(fields["state"].selection)["sale"] == "Sales Order"
    assert fields["partner_id"].relation == "res.partner"
