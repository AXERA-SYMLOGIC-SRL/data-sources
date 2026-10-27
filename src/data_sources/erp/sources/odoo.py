"""Odoo (16 and later, Community or Enterprise, self-hosted or Odoo.sh), read-only.

Connection (`auth.credentials`): `url` (the instance), `database`, `api_key` (a user's API key:
Preferences → Account Security → New API Key) and, for Odoo before 19 only, `username` (that
user's login). Odoo 19 is read through the JSON-2 API (`POST /json/2/<model>/<method>`, the key
as a bearer token); older versions through `execute_kw` over JSON-RPC, which Odoo removes in
version 22. The user's access rights apply: what they can't read isn't synced.

Every read includes archived records and every company the user may access. Records are read
oldest change first (`write_date`); `raw` holds every stored field of the record except one2many
lists, binaries, HTML bodies and the chatter, custom (`x_`) fields included.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from data_sources.erp.base import (
    Company,
    Cursor,
    ErpRecord,
    ErpSource,
    NativeField,
    Page,
    SourceInfo,
)
from data_sources.erp.catalog import ENTITIES, Column
from data_sources.reports.errors import Problem, ReportError
from data_sources.reports.http import ApiClient, Retry, Verdict, exponential

UNREACHABLE = Problem("unreachable", "Odoo could not be reached at that URL.")
BUSY = Problem("unavailable", "Odoo is unavailable or overloaded; try again.")
INVALID = Problem("unavailable", "Odoo returned an invalid response.")
AUTH_FAILED = Problem(
    "auth_failed", "Odoo refused the API key (wrong, expired or revoked) or the user's access."
)
NO_DATABASE = Problem("database_not_found", "Odoo has no database by that name at this URL.")
REFUSED = Problem("request_failed", "Odoo refused the request.")
NO_USERNAME = Problem("request_failed", "Odoo before version 19 also needs the user's login.")

INVOICE_TYPES = ["out_invoice", "out_refund", "in_invoice", "in_refund"]
#: Fields never copied into `raw`: lists of child ids, files, HTML bodies, the chatter and
#: activity
#: widgets.
SKIPPED_TYPES = {"one2many", "binary", "html", "properties", "properties_definition"}
SKIPPED_PREFIXES = ("message_", "activity_", "website_message", "rating_", "__")
#: Never copied out of the ERP: portal access tokens, signup tokens, passwords, signatures.
SECRET_WORDS = ("token", "password", "secret", "signature", "api_key")


@dataclass(frozen=True)
class Derived:
    """A core column computed from native fields."""

    fields: tuple[str, ...]
    compute: Callable[[Mapping[str, Any]], Any]


Source = str | Derived


@dataclass(frozen=True)
class OdooEntity:
    model: str
    columns: Mapping[str, Source]
    domain: tuple[Any, ...] = field(default_factory=tuple)

    @property
    def native(self) -> set[str]:
        names: set[str] = set()
        for source in self.columns.values():
            names.update(source.fields if isinstance(source, Derived) else (source,))
        return names


def _name(value: Any) -> Any:
    """The display name of a many2one (`[id, name]`)."""
    return value[1] if isinstance(value, list) and len(value) == 2 else None


def _signed_total(record: Mapping[str, Any]) -> float | None:
    total = record.get("amount_total_signed")
    if not isinstance(total, int | float):
        return None
    return -abs(total) if "refund" in str(record.get("move_type")) else abs(total)


def _product_type(record: Mapping[str, Any]) -> str | None:
    if record.get("is_storable"):
        return "goods"
    return {"consu": "consumable", "service": "service", "combo": "combo"}.get(
        str(record.get("type"))
    )


def _lead_status(record: Mapping[str, Any]) -> str:
    status = record.get("won_status")
    if status in ("won", "lost"):
        return str(status)
    if record.get("active") is False and not record.get("probability"):
        return "lost"
    return "won" if record.get("probability") == 100 else "open"


COMPANY = {"company_id": "company_id"}
ODOO: dict[str, OdooEntity] = {
    "companies": OdooEntity(
        "res.company",
        {
            "name": "name",
            "currency": "currency_id",
            "country": "country_id",
            "parent_id": "parent_id",
        },
    ),
    "currencies": OdooEntity(
        "res.currency", {"name": "name", "symbol": "symbol", "active": "active", "rate": "rate"}
    ),
    "partners": OdooEntity(
        "res.partner",
        {
            "name": "name",
            "is_company": "is_company",
            "parent_id": "parent_id",
            "is_customer": Derived(("customer_rank",), lambda r: (r.get("customer_rank") or 0) > 0),
            "is_supplier": Derived(("supplier_rank",), lambda r: (r.get("supplier_rank") or 0) > 0),
            "email": "email",
            "phone": "phone",
            "city": "city",
            "country": "country_id",
            "vat": "vat",
            "salesperson_id": "user_id",
            "active": "active",
            "created_at": "create_date",
            **COMPANY,
        },
    ),
    "users": OdooEntity(
        "res.users", {"name": "name", "login": "login", "active": "active", **COMPANY}
    ),
    "product_categories": OdooEntity(
        "product.category", {"name": "name", "full_name": "complete_name", "parent_id": "parent_id"}
    ),
    "products": OdooEntity(
        "product.product",
        {
            "name": "name",
            "sku": "default_code",
            "barcode": "barcode",
            "type": Derived(("type", "is_storable"), _product_type),
            "is_stocked": "is_storable",
            "category_id": "categ_id",
            "list_price": "list_price",
            "cost": "standard_price",
            "uom": "uom_id",
            "can_be_sold": "sale_ok",
            "can_be_purchased": "purchase_ok",
            "active": "active",
            **COMPANY,
        },
    ),
    "warehouses": OdooEntity("stock.warehouse", {"name": "name", "code": "code", **COMPANY}),
    "locations": OdooEntity(
        "stock.location",
        {
            "name": "name",
            "full_name": "complete_name",
            "usage": "usage",
            "warehouse_id": "warehouse_id",
            "active": "active",
            **COMPANY,
        },
    ),
    "sale_orders": OdooEntity(
        "sale.order",
        {
            "number": "name",
            "partner_id": "partner_id",
            "date": "date_order",
            "state": "state",
            "invoice_status": "invoice_status",
            "delivery_status": "delivery_status",
            "salesperson_id": "user_id",
            "team": "team_id",
            "currency": "currency_id",
            "amount_untaxed": "amount_untaxed",
            "amount_tax": "amount_tax",
            "amount_total": "amount_total",
            "margin": "margin",
            "origin": "origin",
            "created_at": "create_date",
            **COMPANY,
        },
    ),
    "sale_order_lines": OdooEntity(
        "sale.order.line",
        {
            "order_id": "order_id",
            "product_id": "product_id",
            "description": "name",
            "quantity": "product_uom_qty",
            "quantity_delivered": "qty_delivered",
            "quantity_invoiced": "qty_invoiced",
            "unit_price": "price_unit",
            "discount": "discount",
            "amount_untaxed": "price_subtotal",
            "amount_total": "price_total",
            "unit_cost": "purchase_price",
            "margin": "margin",
            "currency": "currency_id",
            "state": "state",
            **COMPANY,
        },
        domain=(("display_type", "=", False),),
    ),
    "purchase_orders": OdooEntity(
        "purchase.order",
        {
            "number": "name",
            "partner_id": "partner_id",
            "date": "date_order",
            "date_approved": "date_approve",
            "date_planned": "date_planned",
            "state": "state",
            "receipt_status": "receipt_status",
            "invoice_status": "invoice_status",
            "buyer_id": "user_id",
            "currency": "currency_id",
            "amount_untaxed": "amount_untaxed",
            "amount_tax": "amount_tax",
            "amount_total": "amount_total",
            "origin": "origin",
            **COMPANY,
        },
    ),
    "purchase_order_lines": OdooEntity(
        "purchase.order.line",
        {
            "order_id": "order_id",
            "product_id": "product_id",
            "description": "name",
            "quantity": "product_qty",
            "quantity_received": "qty_received",
            "quantity_billed": "qty_invoiced",
            "unit_price": "price_unit",
            "amount_untaxed": "price_subtotal",
            "amount_total": "price_total",
            "date_planned": "date_planned",
            "currency": "currency_id",
            "state": "state",
            **COMPANY,
        },
        domain=(("display_type", "=", False),),
    ),
    "invoices": OdooEntity(
        "account.move",
        {
            "number": "name",
            "direction": Derived(
                ("move_type",),
                lambda r: "out" if str(r.get("move_type")).startswith("out") else "in",
            ),
            "kind": Derived(
                ("move_type",),
                lambda r: "refund" if "refund" in str(r.get("move_type")) else "invoice",
            ),
            "partner_id": "partner_id",
            "invoice_date": "invoice_date",
            "due_date": "invoice_date_due",
            "accounting_date": "date",
            "state": "state",
            "payment_state": "payment_state",
            "currency": "currency_id",
            "amount_untaxed": "amount_untaxed",
            "amount_tax": "amount_tax",
            "amount_total": "amount_total",
            "amount_due": "amount_residual",
            "amount_total_company": Derived(("amount_total_signed", "move_type"), _signed_total),
            "origin": "invoice_origin",
            "salesperson_id": "invoice_user_id",
            **COMPANY,
        },
        domain=(("move_type", "in", INVOICE_TYPES),),
    ),
    "invoice_lines": OdooEntity(
        "account.move.line",
        {
            "invoice_id": "move_id",
            "product_id": "product_id",
            "description": "name",
            "quantity": "quantity",
            "unit_price": "price_unit",
            "discount": "discount",
            "amount_untaxed": "price_subtotal",
            "amount_total": "price_total",
            "account_id": "account_id",
            "currency": "currency_id",
            **COMPANY,
        },
        domain=(("display_type", "=", "product"), ("move_id.move_type", "in", INVOICE_TYPES)),
    ),
    "accounts": OdooEntity(
        "account.account",
        {"code": "code", "name": "name", "type": "account_type", "reconcile": "reconcile"},
    ),
    "journal_items": OdooEntity(
        "account.move.line",
        {
            "entry": "move_name",
            "date": "date",
            "account_id": "account_id",
            "partner_id": "partner_id",
            "label": "name",
            "journal": "journal_id",
            "debit": "debit",
            "credit": "credit",
            "balance": "balance",
            "state": "parent_state",
            "reconciled": "reconciled",
            **COMPANY,
        },
        domain=(("display_type", "not in", ["line_section", "line_note"]),),
    ),
    "payments": OdooEntity(
        "account.payment",
        {
            "number": "name",
            "partner_id": "partner_id",
            "date": "date",
            "direction": "payment_type",
            "partner_type": "partner_type",
            "amount": "amount",
            "currency": "currency_id",
            "state": "state",
            "journal": "journal_id",
            "memo": "memo",
            **COMPANY,
        },
    ),
    "stock_pickings": OdooEntity(
        "stock.picking",
        {
            "number": "name",
            "type": "picking_type_code",
            "partner_id": "partner_id",
            "origin": "origin",
            "sale_order_id": "sale_id",
            "purchase_order_id": "purchase_id",
            "scheduled_date": "scheduled_date",
            "done_date": "date_done",
            "state": "state",
            "location_id": "location_id",
            "location_dest_id": "location_dest_id",
            **COMPANY,
        },
    ),
    "stock_moves": OdooEntity(
        "stock.move",
        {
            "picking_id": "picking_id",
            "product_id": "product_id",
            "date": "date",
            "quantity": "quantity",
            "uom": "product_uom",
            "location_id": "location_id",
            "location_dest_id": "location_dest_id",
            "state": "state",
            "reference": "reference",
            "origin": "origin",
            "unit_cost": "price_unit",
            "sale_line_id": "sale_line_id",
            "purchase_line_id": "purchase_line_id",
            "production_id": "production_id",
            "raw_material_production_id": "raw_material_production_id",
            **COMPANY,
        },
    ),
    "stock_quants": OdooEntity(
        "stock.quant",
        {
            "product_id": "product_id",
            "location_id": "location_id",
            "quantity": "quantity",
            "reserved_quantity": "reserved_quantity",
            "lot": "lot_id",
            "value": "value",
            **COMPANY,
        },
    ),
    "manufacturing_orders": OdooEntity(
        "mrp.production",
        {
            "number": "name",
            "product_id": "product_id",
            "quantity": "product_qty",
            "quantity_produced": "qty_produced",
            "state": "state",
            "date_start": "date_start",
            "date_finished": "date_finished",
            "origin": "origin",
            "responsible_id": "user_id",
            **COMPANY,
        },
    ),
    "crm_leads": OdooEntity(
        "crm.lead",
        {
            "name": "name",
            "type": "type",
            "partner_id": "partner_id",
            "stage": "stage_id",
            "status": Derived(("won_status", "active", "probability"), _lead_status),
            "expected_revenue": "expected_revenue",
            "currency": "company_currency",
            "probability": "probability",
            "salesperson_id": "user_id",
            "team": "team_id",
            "lost_reason": "lost_reason_id",
            "deadline": "date_deadline",
            "created_at": "create_date",
            "closed_at": "date_closed",
            **COMPANY,
        },
    ),
    "employees": OdooEntity(
        "hr.employee",
        {
            "name": "name",
            "job_title": "job_title",
            "department": "department_id",
            "manager_id": "parent_id",
            "user_id": "user_id",
            "active": "active",
            **COMPANY,
        },
    ),
    "projects": OdooEntity(
        "project.project",
        {
            "name": "name",
            "partner_id": "partner_id",
            "manager_id": "user_id",
            "active": "active",
            **COMPANY,
        },
    ),
    "tasks": OdooEntity(
        "project.task",
        {
            "name": "name",
            "project_id": "project_id",
            "stage": "stage_id",
            "state": "state",
            "allocated_hours": "allocated_hours",
            "effective_hours": "effective_hours",
            "deadline": "date_deadline",
            "partner_id": "partner_id",
            "created_at": "create_date",
            **COMPANY,
        },
    ),
    "timesheets": OdooEntity(
        "account.analytic.line",
        {
            "date": "date",
            "employee_id": "employee_id",
            "project_id": "project_id",
            "task_id": "task_id",
            "description": "name",
            "hours": "unit_amount",
            "cost": "amount",
            **COMPANY,
        },
        domain=(("project_id", "!=", False),),
    ),
    "expenses": OdooEntity(
        "hr.expense",
        {
            "description": "name",
            "employee_id": "employee_id",
            "product_id": "product_id",
            "date": "date",
            "amount": "total_amount_currency",
            "currency": "currency_id",
            "state": "state",
            **COMPANY,
        },
    ),
    "pos_orders": OdooEntity(
        "pos.order",
        {
            "number": "name",
            "shop": "config_id",
            "date": "date_order",
            "partner_id": "partner_id",
            "state": "state",
            "amount_total": "amount_total",
            "amount_tax": "amount_tax",
            "amount_paid": "amount_paid",
            "currency": "currency_id",
            "salesperson_id": "user_id",
            **COMPANY,
        },
    ),
    "pos_order_lines": OdooEntity(
        "pos.order.line",
        {
            "order_id": "order_id",
            "product_id": "product_id",
            "quantity": "qty",
            "unit_price": "price_unit",
            "discount": "discount",
            "amount_untaxed": "price_subtotal",
            "amount_total": "price_subtotal_incl",
            **COMPANY,
        },
    ),
}


def parse_datetime(value: Any) -> dt.datetime | None:
    """Odoo's `YYYY-MM-DD HH:MM:SS` (UTC)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.UTC)
    except ValueError:
        return None


def parse_date(value: Any) -> dt.date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def convert(column: Column, value: Any) -> Any:
    """A native value as the catalog column's type. Odoo says `False` for every empty value."""
    if value is False or value is None:
        return False if column.type == "boolean" and value is False else None
    if column.type == "ref":
        if isinstance(value, list) and value:
            return str(value[0])
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else None
    if column.type == "text":
        return _name(value) if isinstance(value, list) else str(value)
    if column.type in ("number", "integer"):
        return value if isinstance(value, int | float) and not isinstance(value, bool) else None
    if column.type == "boolean":
        return bool(value)
    if column.type == "datetime":
        return parse_datetime(value)
    if column.type == "date":
        return parse_date(value)
    return value


def classify(response: httpx.Response) -> Verdict:
    """JSON-2 answers with HTTP statuses; an unknown database is a 404 HTML page (an unknown model a
    404 JSON error). JSON-RPC answers 200 with an `error` object."""
    if response.status_code == 429 or response.status_code >= 500:
        return Retry(BUSY)
    if response.status_code in (401, 403):
        return AUTH_FAILED
    if response.status_code == 404:
        is_json = response.headers.get("content-type", "").startswith("application/json")
        return REFUSED if is_json else NO_DATABASE
    if response.is_error:
        return REFUSED
    if response.request.url.path.endswith("/jsonrpc"):
        try:
            body = response.json()
        except ValueError:
            return INVALID
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            name = str((error.get("data") or {}).get("name", ""))
            if "AccessDenied" in name or "AccessError" in name or "SessionExpired" in name:
                return AUTH_FAILED
            if (
                "KeyError" in name
                or "OperationalError" in name
                and "database" in str(error).lower()
            ):
                return NO_DATABASE
            return REFUSED
    return None


class OdooSource(ErpSource):
    provider = "odoo"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._api: str | None = None
        self._uid: int | None = None
        self._companies: list[int] | None = None
        self._fields: dict[str, dict[str, dict[str, Any]]] = {}

    @property
    def base_url(self) -> str:
        url = self.credential("url").rstrip("/")
        return url if url.startswith(("http://", "https://")) else f"https://{url}"

    def _client(self) -> ApiClient:
        return ApiClient(
            classify,
            unreachable=UNREACHABLE,
            invalid=INVALID,
            base_url=self.base_url,
            timeout=120,
            attempts=4,
            backoff=exponential(1.0),
            transport=self.transport,
            sleep=self.sleep,
        )

    async def _body(self, api: ApiClient, method: str, url: str, **kwargs: Any) -> Any:
        response = await api.request(method, url, retry=True, **kwargs)
        try:
            return response.json()
        except ValueError:
            raise INVALID.error() from None

    async def _jsonrpc(self, api: ApiClient, service: str, method: str, *args: Any) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
        }
        body = await self._body(api, "POST", "/jsonrpc", json=payload)
        if not isinstance(body, dict) or "result" not in body:
            raise INVALID.error()
        return body["result"]

    async def _flavour(self, api: ApiClient) -> tuple[str, str]:
        """The server's version and the API to read it with (`json2` from Odoo 19)."""
        body = await self._body(
            api,
            "POST",
            "/web/webclient/version_info",
            json={"jsonrpc": "2.0", "method": "call", "params": {}},
        )
        info = body.get("result") if isinstance(body, dict) else None
        if not isinstance(info, dict):
            raise INVALID.error()
        version = str(info.get("server_version", ""))
        major = (info.get("server_version_info") or [0])[0]
        return version, "json2" if isinstance(major, int) and major >= 19 else "jsonrpc"

    async def _call(self, api: ApiClient, model: str, method: str, **kwargs: Any) -> Any:
        """`model.method(**kwargs)` as the user, over whichever API the server speaks."""
        if self._api is None:
            _, self._api = await self._flavour(api)
        context = {
            "active_test": False,
            **({"allowed_company_ids": self._companies} if self._companies else {}),
        }
        if self._api == "json2":
            return await self._body(
                api,
                "POST",
                f"/json/2/{model}/{method}",
                json={**kwargs, "context": context},
                headers={
                    "Authorization": f"bearer {self.credential('api_key')}",
                    "X-Odoo-Database": self.credential("database"),
                },
            )
        if self._uid is None:
            username = self.credential("username")
            if not username:
                raise NO_USERNAME.error()
            uid = await self._jsonrpc(
                api,
                "common",
                "authenticate",
                self.credential("database"),
                username,
                self.credential("api_key"),
                {},
            )
            if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
                raise AUTH_FAILED.error()
            self._uid = uid
        return await self._jsonrpc(
            api,
            "object",
            "execute_kw",
            self.credential("database"),
            self._uid,
            self.credential("api_key"),
            model,
            method,
            [],
            {**kwargs, "context": context},
        )

    async def _ensure_companies(self, api: ApiClient) -> None:
        if self._companies is None:
            found = await self._call(api, "res.company", "search_read", domain=[], fields=["id"])
            self._companies = sorted(
                c["id"] for c in found if isinstance(c, dict) and isinstance(c.get("id"), int)
            )

    async def check(self) -> SourceInfo:
        if not self.configured:
            raise ReportError("not_configured", "Fill in the URL, the database and the API key.")
        async with self._client() as api:
            version, self._api = await self._flavour(api)
            if self._api == "json2":
                context = await self._call(api, "res.users", "context_get")
                uid = context.get("uid") if isinstance(context, dict) else None
            else:
                await self._call(api, "res.users", "search_count", domain=[])
                uid = self._uid
            users = await self._call(
                api, "res.users", "search_read", domain=[["id", "=", uid]], fields=["name", "login"]
            )
            companies = await self._call(
                api, "res.company", "search_read", domain=[], fields=["name", "currency_id"]
            )
        if not isinstance(users, list) or not users or not isinstance(companies, list):
            raise INVALID.error()
        self._companies = sorted(c["id"] for c in companies)
        return SourceInfo(
            version=version,
            api=self._api,
            user=str(users[0].get("login") or users[0].get("name")),
            companies=[
                Company(
                    str(c["id"]), str(c.get("name", "")), str(_name(c.get("currency_id")) or "")
                )
                for c in companies
            ],
        )

    async def entities(self) -> list[str]:
        async with self._client() as api:
            found = await self._call(
                api,
                "ir.model",
                "search_read",
                domain=[["model", "in", sorted({e.model for e in ODOO.values()})]],
                fields=["model"],
            )
        installed = {m["model"] for m in found if isinstance(m, dict)}
        return [name for name, spec in ODOO.items() if spec.model in installed]

    async def _model_fields(self, api: ApiClient, model: str) -> dict[str, dict[str, Any]]:
        if model not in self._fields:
            found = await self._call(
                api, model, "fields_get", attributes=["string", "type", "relation", "store"]
            )
            if not isinstance(found, dict):
                raise INVALID.error()
            self._fields[model] = found
        return self._fields[model]

    async def describe(self, entity: str) -> list[NativeField]:
        spec = self._spec(entity)
        async with self._client() as api:
            fields = await self._model_fields(api, spec.model)
        return [
            NativeField(
                name,
                str(f.get("string", name)),
                str(f.get("type", "")),
                f.get("relation"),
                bool(f.get("store")),
            )
            for name, f in sorted(fields.items())
        ]

    def _spec(self, entity: str) -> OdooEntity:
        if entity not in ODOO:
            raise ReportError("request_failed", f"Odoo has no mapping for {entity!r}.")
        return ODOO[entity]

    def _read_fields(self, spec: OdooEntity, fields: dict[str, dict[str, Any]]) -> list[str]:
        raw = {
            name
            for name, f in fields.items()
            if f.get("store")
            and f.get("type") not in SKIPPED_TYPES
            and not name.startswith(SKIPPED_PREFIXES)
            and not any(word in name for word in SECRET_WORDS)
        }
        # Core columns may come from non-stored (computed) fields: they're read too, when the
        # model
        # has them.
        return sorted(raw | {n for n in spec.native if n in fields} | {"write_date"})

    async def count(self, entity: str) -> int:
        spec = self._spec(entity)
        async with self._client() as api:
            await self._ensure_companies(api)
            found = await self._call(
                api, spec.model, "search_count", domain=list(map(list, spec.domain))
            )
        return int(found)

    async def read(self, entity: str, cursor: Cursor | None = None, page_size: int = 1000) -> Page:
        spec, columns = self._spec(entity), ENTITIES[entity].columns
        domain = [list(term) for term in spec.domain]
        if cursor is not None:
            domain.append(
                [
                    "write_date",
                    ">=",
                    cursor.updated_at.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M:%S"),
                ]
            )
        async with self._client() as api:
            await self._ensure_companies(api)
            fields = await self._model_fields(api, spec.model)
            rows = await self._call(
                api,
                spec.model,
                "search_read",
                domain=domain,
                fields=self._read_fields(spec, fields),
                offset=cursor.offset if cursor else 0,
                limit=page_size,
                order="write_date asc, id asc",
            )
        if not isinstance(rows, list):
            raise INVALID.error()
        records = [
            self._record(entity, spec, columns, row) for row in rows if isinstance(row, dict)
        ]
        if len(rows) < page_size or not records:
            return Page(records, None)
        last = records[-1].updated_at
        if cursor is not None and last == cursor.updated_at:
            return Page(records, Cursor(last, cursor.offset + len(records)))
        return Page(records, Cursor(last, sum(1 for r in records if r.updated_at == last)))

    def _record(
        self, entity: str, spec: OdooEntity, columns: tuple[Column, ...], row: dict[str, Any]
    ) -> ErpRecord:
        values: dict[str, Any] = {}
        for column in columns:
            source = spec.columns.get(column.name)
            if source is None:
                values[column.name] = None
                continue
            native = source.compute(row) if isinstance(source, Derived) else row.get(source)
            values[column.name] = native if isinstance(source, Derived) else convert(column, native)
            if column.name_column and not isinstance(source, Derived):
                values[column.name_column] = _name(native)
        updated = parse_datetime(row.get("write_date"))
        if updated is None:
            raise ReportError(
                "unavailable", f"Odoo returned a {entity} record without its change time."
            )
        return ErpRecord(str(row["id"]), updated, values, row)

    async def list_ids(self, entity: str) -> list[str]:
        spec = self._spec(entity)
        async with self._client() as api:
            await self._ensure_companies(api)
            found = await self._call(api, spec.model, "search", domain=list(map(list, spec.domain)))
        if not isinstance(found, list):
            raise INVALID.error()
        return [str(i) for i in found]
