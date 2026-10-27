# ERP sources (`data_sources.erp`)

Read-only adapters for ERPs: the records of a business (parties, products, orders and their lines, invoices,
payments, the general ledger, stock, production, CRM, projects, people) in **one vocabulary for every ERP**, read
**incrementally**, for an application that keeps its own copy. Odoo is the first adapter.

ERPs share no API, so the contract is the **catalog** (`catalog.py`): canonical entities with core columns. Each
adapter maps the entities it has onto its own objects and keeps the complete native record beside them.

## Use

```python
from data_sources.config.schema import AuthConfig, ConnectorConfig
from data_sources.erp import create_erp_source

source = create_erp_source(
    ConnectorConfig(
        provider="odoo",
        auth=AuthConfig(
            type="api_key",
            credentials={"url": "https://shop.example.com", "database": "shop", "api_key": key},
        ),
    )
)
info = await source.check()            # SourceInfo(version, api, user, companies)
for entity in await source.entities():  # the catalog entities this instance has
    cursor = None                       # or the last cursor stored: only what changed since
    while True:
        page = await source.read(entity, cursor)  # Page(records: [ErpRecord], next_cursor)
        store(page.records)                       # ErpRecord(id, updated_at, values, raw)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
ids_now = await source.list_ids("sale_orders")  # what you store and isn't here was deleted
```

- `values`: the catalog's core columns for the entity. A `ref` column holds the other record's id (text) and the
  adapter fills `<name without _id>_name` with its display name when the ERP gives one.
- `raw`: the native record, except child-id lists, binaries, HTML bodies, chatter fields, and anything whose name
  contains `token`, `password`, `secret`, `signature` or `api_key` (Odoo's portal `access_token` must not leave it).
- **Cursor** `(updated_at, offset)`: records changed at or after `updated_at`, skipping the first `offset` of that
  second. ERPs report change times in whole seconds and bulk operations change thousands of records in one second,
  so a `(time, id)` keyset would lose records. To resume later, start from a stored time (a little earlier, offset 0):
  re-reading a few records is harmless when you upsert by id.
- **Deletions**: ERPs don't report them; compare `list_ids` with what you hold.
- **Errors**: `data_sources.reports.ReportError` with `reason` `auth_failed`, `unreachable`, `database_not_found`,
  `unavailable` (retried first), `request_failed`, `not_configured`. Messages never hold the key or the ERP's
  response (Odoo puts a server traceback in its errors).

## The catalog

29 entities: `companies`, `currencies`, `partners` (customers and vendors in one), `users`, `product_categories`,
`products`, `warehouses`, `locations`, `sale_orders`, `sale_order_lines`, `purchase_orders`, `purchase_order_lines`,
`invoices` (customer and vendor, `direction` out/in, `kind` invoice/refund), `invoice_lines`, `accounts`,
`journal_items` (the general ledger), `payments`, `stock_pickings`, `stock_moves`, `stock_quants` (stock on hand),
`manufacturing_orders`, `crm_leads`, `employees`, `projects`, `tasks`, `timesheets`, `expenses`, `pos_orders`,
`pos_order_lines`. Every entity also has `id` and `updated_at`. Every column has a description (the conventions are
in `COMMON`: amounts are in the document's `currency` and never negative, a credit note is told apart by `kind`;
`_company` amounts are in the company's currency and signed).

It is close to what unified accounting and commerce APIs converge on (Merge, Codat, Apideck: contacts, items,
invoices with direction, payments, accounts, journal entries, orders), extended with stock, production, CRM and
projects. Add a column when a second ERP can fill it too; an ERP-only field stays in `raw`.

## Odoo (`sources/odoo.py`)

- **Connection:** `url`, `database`, `api_key` (Odoo: avatar → My Profile → Account Security → New API Key) and, for
  Odoo before 19 only, `username`. The user's access rights apply.
- **API:** Odoo 19+ through JSON-2 (`POST /json/2/<model>/<method>`, `Authorization: bearer <key>`,
  `X-Odoo-Database`); 16–18 through `execute_kw` over `/jsonrpc` (`common.authenticate` with the login and key first).
  The version comes from `/web/webclient/version_info`. Odoo plans to remove `/xmlrpc` and `/jsonrpc` in Odoo 22
  (Odoo Online: 21.1); JSON-2 on Odoo Online needs a custom plan.
- **Reads:** `search_read` with every stored field (minus the skipped ones above), ordered `write_date, id`,
  `active_test=False` (archived records included) and every company the user may access (`allowed_company_ids`).
  An unknown database is a 404 HTML page (`database_not_found`); an unknown model a 404 JSON error.
- **Mapping:** `ODOO` maps each entity to a model, a domain (invoices are `account.move` of four `move_type`s;
  invoice lines are their `display_type = product` lines) and its columns (a native field, or `Derived` for
  computed ones: `direction`, `kind`, a lead's `status`, a product's `type`). An entity whose model isn't installed
  isn't in `entities()`.
- Measured on a local Odoo 19 with a year of seeded history (≈30,000 records over 29 entities): a full read in
  ≈27 s, ≈1,100 records/s at 1,000 per page.

## Adding an ERP

A module `data_sources/erp/sources/<provider>.py` with an `ErpSource` subclass (`provider`, `app_credentials`, and
`check`, `entities`, `describe`, `count`, `read`, `list_ids`); the registry finds it, nothing is registered by hand.
Map the catalog entities it has onto its objects; keep requests on `reports.http.ApiClient` with a `classify` for its
error shapes; test it on `httpx.MockTransport` (see `tests/test_erp.py`, which fakes Odoo 19 and 18).

What changes per ERP is only what the adapter hides:

| | Odoo | ERPNext / Frappe | Dolibarr |
|---|---|---|---|
| Records | `search_read` on a model with a domain | `/api/resource/<DocType>` with `filters` | `/api/index.php/<object>` with `sqlfilters` |
| Change time | `write_date` | `modified` | `tms` |
| Deletions | none (compare ids) | `Deleted Document` doctype | none (compare ids) |
| References | `[id, name]` | the linked document's `name` | `fk_*` / `socid` ids |
| Lines | their own model | child tables inside the document | `lines[]` inside the document |
| Auth | `bearer <api key>` | `token key:secret` | `DOLAPIKEY` header |
