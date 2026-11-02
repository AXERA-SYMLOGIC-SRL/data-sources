# ERP sources (`data_sources.erp`)

Read-only adapters for ERPs: a business's own records (parties, products, orders and their lines, invoices,
payments, the general ledger, stock, production, CRM, projects, people), read **incrementally** and returned **as the
ERP answers them**, for an application that keeps its own copy. Odoo is the first adapter.

Like a report source, an ERP source reads what the application asks for, described by a **spec in the ERP's own
terms** (for Odoo `{"model": "sale.order"}`); what the records mean, and which of their fields matter, is the
application's business. (An earlier version mapped every ERP onto one catalog of 29 canonical entities; it renamed
Odoo's fields, hid everything the catalog didn't cover, and was retired.)

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
specs = {"sale_order": {"model": "sale.order"}, "sale_order_line": {"model": "sale.order.line"}}
async with source:                           # one connection (and one sign-in) for the whole sync
    info = await source.check()              # SourceInfo(version, api, user, companies)
    for name in await source.available(specs):  # installed, and readable by the user
        cursor = None                        # or the last cursor stored: only what changed since
        while True:
            page = await source.read(specs[name], cursor)  # Page(records: [ErpRecord], next_cursor)
            store(page.records)                            # ErpRecord(id, updated_at, raw)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
    ids_now = await source.list_ids(specs["sale_order"])  # what you store and isn't here was deleted
    fields = await source.fields(specs["sale_order"])     # NativeField(name, label, type, relation, stored,
                                                          #   selection, help, readable), custom ones included
    more = await source.models()                          # ErpModel(name, label, modules): what else it holds
```

- `raw`: the record as the ERP returned it, minus child-id lists, binaries, HTML bodies and anything
  whose name contains a `SECRET_WORDS` word (`token`, `password`, `secret`, `signature`, `api_key`: Odoo's portal
  `access_token` must not leave it); for Odoo also text fields named like a key, a client id or an IP
  (`CREDENTIAL_NAME`: a kiosk key, ANAF's e-Factura `key_download`, a printer's IP), and the chatter's links.
- **Cursor** `(updated_at, offset)`: records changed at or after `updated_at`, skipping the first `offset` of that
  second. ERPs report change times in whole seconds and bulk operations change thousands of records in one second;
  Odoo even stores microseconds it doesn't return, so `write_date = t` matches nothing and a `(time, id)` keyset would
  lose records. To resume later, start from a stored time (no offset): re-reading a few records is harmless when you
  upsert by id.
- **Deletions**: ERPs don't report them; compare `list_ids` with what you hold.
- **What else is there**: `NativeField.readable` says whether a read that names no fields takes a field (what an
  application can count on finding in `raw`); `models()` lists the kinds of business records the instance holds
  (modules' and the customer's included, technical ones left out), for an application to offer. Empty for an ERP with
  a fixed set.
- **Errors**: `data_sources.reports.ReportError` with `reason` `auth_failed`, `unreachable`, `database_not_found`,
  `access_denied` (the user may not read that model: skip it), `unavailable` (retried first), `request_failed`,
  `not_configured`. Messages never hold the key; `diagnostics` holds the ERP's own error name and message, never its
  traceback.

## Odoo (`sources/odoo.py`)

- **Connection:** `url`, `database`, `api_key` (Odoo: avatar → My Profile → Account Security → New API Key) and
  `username` (the login): needed before Odoo 19, optional after.
- **API:** Odoo 19+ through JSON-2 (`POST /json/2/<model>/<method>`, `Authorization: bearer <key>`,
  `X-Odoo-Database`, named arguments); 16–18 through `execute_kw` over `/jsonrpc` (`common.authenticate` with the
  login and key first). On 19+, a key JSON-2 refuses is tried over JSON-RPC when a login is given, where the secret
  may be a password (Odoo's demo databases). The version comes from `/web/webclient/version_info`. Odoo removes
  `/xmlrpc` and `/jsonrpc` in Odoo 22 (Odoo Online: 21.1); Odoo Online opens the external API on its Custom plan only.
- **Spec:** `{"model", "domain"?, "extra"?, "fields"?}`. Without `fields`, every stored field the user may read is
  asked for (custom `x_` fields included, minus the skipped ones above), plus the `extra` ones the model has
  (non-stored fields the application wants, such as `product.product.list_price`); `fields` asks for exactly those.
- **Models:** `models()` reads `ir.model`: not transient, not abstract (19 and later can say), with a stored
  `write_date` (`ir.model.fields`), and not Odoo's machinery (`TECHNICAL_MODELS`: settings, users and groups, the
  chatter and live chat, the bus, website, reports, ...). 292 on the local Odoo 19 with ~40 apps.
- **Reads:** `search_read` ordered `write_date, id`, with `active_test=False` (archived records included), every
  company the user may access (`allowed_company_ids`) and `lang=en_US`. `available` checks `ir.model` and a
  zero-record `search_count` per model (an `AccessError` means not readable). An unknown database is a 404 HTML page
  (`database_not_found`); an unknown model a 404 JSON error (`request_failed`); a model the user may not read an
  `AccessError` (`access_denied`, not an auth failure).
- **Values** are Odoo's: a many2one is `[id, "display name"]`, a many2many a list of ids, every empty value `false`
  (a `false` boolean too), datetimes `YYYY-MM-DD HH:MM:SS` in UTC.
- Measured: a local Odoo 19 with a year of seeded history, 43 models, 27,873 records in ≈39 s; the hosted Odoo 20
  Enterprise demo, 3,096 records in ≈18 s over the internet (through the JSON-RPC fallback).
- `tests/test_odoo_live.py` (opt-in) runs against a real Odoo: `ODOO_TEST_URL/DB/KEY[/USER]`, or `ODOO_TEST_DEMO=1`
  for a fresh database from `https://demo.odoo.com/start`.

## Adding an ERP

A module `data_sources/erp/sources/<provider>.py` with an `ErpSource` subclass (`provider`, `app_credentials`, and
`check`, `available`, `fields`, `count`, `read`, `list_ids`; `__aenter__`/`__aexit__` to share a connection); the
registry finds it, nothing is registered by hand. Define its spec in its own terms; keep requests on
`reports.http.ApiClient` with a `classify` for its error shapes; test it on `httpx.MockTransport` (see
`tests/test_erp.py`, which fakes Odoo 19 and 18).

| | Odoo | ERPNext / Frappe | Dolibarr |
|---|---|---|---|
| Records | `search_read` on a model with a domain | `/api/resource/<DocType>` with `filters` | `/api/index.php/<object>` with `sqlfilters` |
| Change time | `write_date` | `modified` | `tms` |
| Deletions | none (compare ids) | `Deleted Document` doctype | none (compare ids) |
| References | `[id, name]` | the linked document's `name` | `fk_*` / `socid` ids |
| Lines | their own model | child tables inside the document | `lines[]` inside the document |
| Auth | `bearer <api key>` | `token key:secret` | `DOLAPIKEY` header |
