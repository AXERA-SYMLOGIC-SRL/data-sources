"""What every ERP source implements, so an application can sync any ERP the same way.

A source is an `ErpSource` subclass in a module under `data_sources/erp/sources/`: the registry
(`registry.py`) finds it there, so adding an ERP is dropping in a file. It is built from a
`ConnectorConfig` (`create_erp_source`): `provider` names it, `auth.credentials` holds what
connects to one instance (its URL, database, the user's API key), `options` anything else.

It reads the canonical entities of `catalog.py`, page by page, oldest change first, from a
cursor: the first sync starts with no cursor, a later one from the last cursor it stored, so only
what changed is read again. ERPs don't report deletions, so `list_ids` gives every id an entity
has now, for the caller to compare.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

import httpx

from data_sources.config.schema import ConnectorConfig
from data_sources.reports.http import Sleep

#: Records asked for per page.
PAGE_SIZE = 1000


@dataclass(frozen=True)
class Cursor:
    """Where a sync stopped: records changed at or after `updated_at`, skipping the first `offset`
    of them (those of that very time already read). ERPs report change times in whole seconds
    while many records can change in the same second, so a time alone can't say where a page
    ended. A later sync can start from a stored cursor's time a little earlier, with no offset:
    it reads a few records again, never misses one."""

    updated_at: dt.datetime
    offset: int = 0


@dataclass(frozen=True)
class ErpRecord:
    """One record: its id, when it last changed, the catalog's core columns (`values`, `ref` columns
    as ids plus their `_name`), and the ERP's own record, complete (`raw`)."""

    id: str
    updated_at: dt.datetime
    values: dict[str, Any]
    raw: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class Page:
    records: list[ErpRecord]
    #: Where to continue from; None once the entity is read to the end.
    next_cursor: Cursor | None


@dataclass(frozen=True)
class NativeField:
    """A field of the ERP's own record, as its schema describes it (custom fields included)."""

    name: str
    label: str
    type: str
    relation: str | None = None
    stored: bool = True


@dataclass(frozen=True)
class Company:
    id: str
    name: str
    currency: str


@dataclass(frozen=True)
class SourceInfo:
    """What a connection check found."""

    version: str
    #: The API flavour used (`json2`, `jsonrpc`, ...).
    api: str
    user: str
    companies: list[Company]


class ErpSource(ABC):
    """One ERP. `transport` and `sleep` are for tests: an `httpx` transport, and what it waits with
    between retries."""

    #: The name `create_erp_source` finds it by (`ConnectorConfig.provider`).
    provider: ClassVar[str]
    #: The keys of `auth.credentials` a connection needs (`configured` checks them).
    app_credentials: ClassVar[tuple[str, ...]] = ("url", "database", "api_key")

    def __init__(
        self,
        config: ConnectorConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.config = config
        self.transport = transport
        self.sleep = sleep

    def credential(self, key: str) -> str:
        """One of the connection's credentials, "" when not set."""
        value = self.config.auth.credentials.get(key) if self.config.auth else None
        return str(value).strip() if value else ""

    def option(self, key: str, default: str = "") -> str:
        value = self.config.options.get(key)
        return str(value) if value else default

    @property
    def configured(self) -> bool:
        return all(self.credential(key) for key in self.app_credentials)

    @abstractmethod
    async def check(self) -> SourceInfo:
        """Connect and sign in; what the instance is and which companies the user sees."""

    @abstractmethod
    async def entities(self) -> list[str]:
        """The catalog entities this instance can provide (an ERP without manufacturing has no
        `manufacturing_orders`)."""

    @abstractmethod
    async def describe(self, entity: str) -> list[NativeField]:
        """The native fields behind `entity` (what `raw` holds), custom ones included."""

    @abstractmethod
    async def count(self, entity: str) -> int:
        """How many records `entity` has now (archived included)."""

    @abstractmethod
    async def read(
        self, entity: str, cursor: Cursor | None = None, page_size: int = PAGE_SIZE
    ) -> Page:
        """The next page of `entity`'s records changed after `cursor` (all of them from None),
        oldest change first."""

    @abstractmethod
    async def list_ids(self, entity: str) -> list[str]:
        """Every id `entity` has now (archived included): what is stored and missing here was
        deleted."""
