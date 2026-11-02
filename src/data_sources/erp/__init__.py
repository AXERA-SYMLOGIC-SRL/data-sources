"""ERP sources: an ERP's own records (parties, products, orders, invoices, stock, production, CRM,
projects, people), read incrementally and returned as the ERP answers them.

Every ERP is an `ErpSource` subclass in a module under `data_sources/erp/sources/`, found there
by the registry: adding an ERP is dropping in a file. Build one from a `ConnectorConfig`
(`provider` names it, `auth.credentials` holds the instance's URL, database and the user's API
key) and read what a spec, in the ERP's own terms, describes:

    auth = AuthConfig(type="api_key", credentials={"url": ..., "database": ..., "api_key": ...})
    source = create_erp_source(ConnectorConfig(provider="odoo", auth=auth))
    specs = {"sale_order": {"model": "sale.order"}}
    async with source:                              # one connection for the whole sync
        info = await source.check()                 # version, user, companies
        for name in await source.available(specs):  # installed and readable
            cursor = None
            while True:
                page = await source.read(specs[name], cursor)  # ErpRecord(id, updated_at, raw)
                ...
                if page.next_cursor is None:
                    break
                cursor = page.next_cursor

Errors are `data_sources.reports.ReportError` with a `reason` (`auth_failed`, `unreachable`,
`database_not_found`, `access_denied`, `unavailable`, `request_failed`, `not_configured`).
"""

from data_sources.erp.base import (
    PAGE_SIZE,
    SECRET_WORDS,
    Company,
    Cursor,
    ErpRecord,
    ErpSource,
    NativeField,
    Page,
    SourceInfo,
    Spec,
)
from data_sources.erp.registry import ErpSourceRegistry, create_erp_source, discover, erp_sources

__all__ = [
    "PAGE_SIZE",
    "SECRET_WORDS",
    "Company",
    "Cursor",
    "ErpRecord",
    "ErpSource",
    "ErpSourceRegistry",
    "NativeField",
    "Page",
    "SourceInfo",
    "Spec",
    "create_erp_source",
    "discover",
    "erp_sources",
]
