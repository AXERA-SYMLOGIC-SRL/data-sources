"""ERP sources: the records of an ERP (parties, products, orders, invoices, stock, production, CRM,
projects, people) in one vocabulary for every ERP (`catalog.py`), read incrementally.

Every ERP is an `ErpSource` subclass in a module under `data_sources/erp/sources/`, found there
by the registry: adding an ERP is dropping in a file. Build one from a `ConnectorConfig`
(`provider` names it, `auth.credentials` holds the instance's URL, database and the user's API
key):

    auth = AuthConfig(type="api_key", credentials={"url": ..., "database": ..., "api_key": ...})
    source = create_erp_source(ConnectorConfig(provider="odoo", auth=auth))
    info = await source.check()                    # version, user, companies
    for entity in await source.entities():         # the catalog entities this instance has
        cursor = None
        while True:
            page = await source.read(entity, cursor)   # ErpRecord(id, updated_at, values, raw)
            ...
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

Errors are `data_sources.reports.ReportError` with a `reason` (`auth_failed`, `unreachable`,
`database_not_found`, `unavailable`, `request_failed`, `not_configured`).
"""

from data_sources.erp.base import (
    PAGE_SIZE,
    Company,
    Cursor,
    ErpRecord,
    ErpSource,
    NativeField,
    Page,
    SourceInfo,
)
from data_sources.erp.catalog import CATALOG, ENTITIES, Column, Entity, entity
from data_sources.erp.registry import ErpSourceRegistry, create_erp_source, discover, erp_sources

__all__ = [
    "CATALOG",
    "ENTITIES",
    "PAGE_SIZE",
    "Column",
    "Company",
    "Cursor",
    "Entity",
    "ErpRecord",
    "ErpSource",
    "ErpSourceRegistry",
    "NativeField",
    "Page",
    "SourceInfo",
    "create_erp_source",
    "discover",
    "entity",
    "erp_sources",
]
