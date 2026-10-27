"""What every report source implements, so an application can treat providers alike.

A source is a subclass of `ReportSource` in a module under `data_sources/reports/sources/`: the
registry (`registry.py`) finds it there, so adding a provider is dropping in a file. It is built
from a `ConnectorConfig` (`create_report_source`): `provider` names it, `auth.credentials` holds
the app's own credentials (OAuth client, developer token, ...), `options` anything else.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, ClassVar

import httpx

from data_sources.config.schema import ConnectorConfig
from data_sources.reports.http import Sleep
from data_sources.reports.models import RawReport


@dataclass(frozen=True)
class Grant:
    """What a consent bought. Keep `credentials` secret and pass them back to every call."""

    credentials: dict[str, Any] = field(repr=False)
    #: Who consented, for display (Google's email, the Facebook user's name), when known.
    identity: str | None = None
    #: Seconds until the grant stops working, when the provider says (Meta's tokens).
    expires_in: int | None = None


@dataclass(frozen=True)
class Account:
    """Something a grant can read: a GA4 property, an ad account, an advertiser, a site."""

    #: What `read` takes as `account`.
    id: str
    name: str
    #: What it is listed under (a GA account, the Google Ads manager it is reached through).
    group: str = ""
    currency: str = ""
    timezone: str = ""
    #: Anything else the provider says about it (a permission level, a status).
    details: dict[str, Any] = field(default_factory=dict)


class ReportSource(ABC):
    """One provider's reports. `transport` and `sleep` are for tests: an `httpx` transport, and
    what the source waits with between retries."""

    #: The name `create_report_source` finds it by (`ConnectorConfig.provider`).
    provider: ClassVar[str]
    #: The keys of `auth.credentials` it needs to sign users in (`configured` checks them).
    app_credentials: ClassVar[tuple[str, ...]] = ()

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
        """One of the app's credentials, "" when not set."""
        value = self.config.auth.credentials.get(key) if self.config.auth else None
        return str(value) if value else ""

    def option(self, key: str, default: str = "") -> str:
        value = self.config.options.get(key)
        return str(value) if value else default

    @property
    def configured(self) -> bool:
        """Whether every app credential the consent needs is set."""
        return all(self.credential(key) for key in self.app_credentials)

    @abstractmethod
    def authorize_url(self, state: str) -> str:
        """Where to send the user to consent; the provider redirects back with `state`."""

    @abstractmethod
    async def exchange_code(self, code: str) -> Grant:
        """The grant the consent's `code` buys."""

    @abstractmethod
    async def list_accounts(self, credentials: Mapping[str, Any]) -> list[Account]:
        """What the grant can read."""

    @abstractmethod
    async def read(
        self,
        credentials: Mapping[str, Any],
        account: str,
        spec: Mapping[str, Any],
        since: date,
        until: date,
    ) -> RawReport:
        """The report `spec` describes (its shape is the provider's: dimensions and metrics,
        insight fields, a GAQL resource...) for `account` over `since..until`, inclusive."""
