from __future__ import annotations

import builtins
import re
from collections.abc import AsyncIterator
from typing import Any

from pydantic import ValidationError

from data_sources.config.schema import ConnectorConfig
from data_sources.connectors.google_analytics.client import GoogleAnalyticsClient
from data_sources.connectors.google_analytics.models import (
    PropertySummary,
    ReportResult,
    ReportSpec,
)
from data_sources.connectors.google_drive.auth import credential_from_config
from data_sources.core.connector import Connector
from data_sources.core.exceptions import (
    ConfigurationError,
    ConnectionError,
    DataSourceError,
    NotFoundError,
)
from data_sources.core.logging import logger
from data_sources.core.models import Item, ItemType
from data_sources.core.paths import relative_path
from data_sources.core.registry import register_connector
from data_sources.core.tables import to_csv

#: What a service account asks for when `auth.credentials.scopes` doesn't say otherwise.
DEFAULT_SCOPES = ("https://www.googleapis.com/auth/analytics.readonly",)

#: Rows asked for per request; the API allows up to 250,000.
DEFAULT_PAGE_SIZE = 100_000

_PROPERTY = re.compile(r"^(?:properties/)?(\d+)$")

CSV_MIME = "text/csv"


def parse_property(value: str) -> str:
    """`properties/{id}` from a bare numeric property id or that form — `ConfigurationError`
    for anything else (a Measurement ID like `G-XXXX` or a stream id isn't a property)."""
    match = _PROPERTY.match(value.strip())
    if match is None:
        raise ConfigurationError(
            f"google_analytics options.property must be a GA4 property id such as "
            f"'123456789' or 'properties/123456789', got {value!r}"
        )
    return f"properties/{match.group(1)}"


def _report_specs(raw: Any) -> dict[str, ReportSpec]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigurationError("google_analytics options.reports must map a name to a report")
    specs: dict[str, ReportSpec] = {}
    for name, definition in raw.items():
        if not name or "/" in name:
            raise ConfigurationError(f"google_analytics report name {name!r} is not usable")
        try:
            specs[str(name)] = ReportSpec.model_validate(definition)
        except ValidationError as exc:
            raise ConfigurationError(f"google_analytics report {name!r} is invalid: {exc}") from exc
    return specs


@register_connector("google_analytics")
class GoogleAnalyticsConnector(Connector):
    """Connector for one Google Analytics 4 property, read through the Data API.

    Analytics holds report tables, not files, so the connector offers both views:

    - `run_report(spec)` runs any report ad hoc and returns its rows (`ReportResult`).
    - `options.reports` names reports (`{name: ReportSpec-shaped dict}`); each is exposed
      as the file `<name>.csv` — `list()` yields them, `get_metadata` describes one, and
      `download` runs it and streams the table as CSV, page by page.

    Addressed via `config.options["property"]`: the GA4 property id (bare or
    `properties/{id}`). It may be left out to connect first and choose after: `list_properties()`
    lists the properties the credential can see, and until one is configured only
    `validate()` and `list_properties()` work. Auth is `type="oauth"` or
    `type="service_account"`, as for Google Drive (`google_drive.auth.credential_from_config`),
    but asking for the `analytics.readonly` scope; a service account must be added as a viewer
    on the property.
    `options.page_size` sets rows per request (default 100,000).

    The listing is flat: a path other than the root is refused (`NotFoundError`).
    Read-only — no sync, webhooks or permissions.
    """

    provider = "google_analytics"

    def __init__(self, config: ConnectorConfig) -> None:
        super().__init__(config)
        prop = config.options.get("property")
        self._configured_property = parse_property(str(prop)) if prop else None
        self._reports = _report_specs(config.options.get("reports"))
        self._page_size = int(config.options.get("page_size", DEFAULT_PAGE_SIZE))
        if not 1 <= self._page_size <= 250_000:
            raise ConfigurationError("google_analytics options.page_size must be 1 to 250000")
        self._client: GoogleAnalyticsClient | None = None
        self.logger = logger.getChild("google_analytics.connector")

    @property
    def client(self) -> GoogleAnalyticsClient:
        if self._client is None:
            raise ConnectionError(
                f"{self.provider} connector is not connected; call connect() first"
            )
        return self._client

    @property
    def _property(self) -> str:
        if self._configured_property is None:
            raise ConfigurationError(
                "google_analytics connector has no options.property; pick one from "
                "list_properties() first"
            )
        return self._configured_property

    async def connect(self) -> None:
        credential = credential_from_config(
            self.config.auth, provider=self.provider, default_scopes=DEFAULT_SCOPES
        )
        self._client = GoogleAnalyticsClient(credential)

    async def validate(self) -> bool:
        try:
            if self._configured_property is None:
                await self.client.list_account_summaries()
            else:
                await self.client.get_metadata(self._configured_property)
        except DataSourceError as exc:
            self.logger.warning(f"validate() failed for {self._configured_property}: {exc}")
            return False
        return True

    async def list_properties(self) -> builtins.list[PropertySummary]:
        """Every GA4 property the credential can see (Admin API; the `analytics.readonly`
        scope covers it, and the project needs the Google Analytics Admin API enabled)."""
        return [
            PropertySummary(
                property=prop["property"],
                display_name=prop.get("displayName", ""),
                account=account["account"],
                account_name=account.get("displayName", ""),
            )
            for account in await self.client.list_account_summaries()
            for prop in account.get("propertySummaries", [])
        ]

    async def _pages(self, spec: ReportSpec) -> AsyncIterator[dict[str, Any]]:
        """The report's response pages, until every row — or `spec.limit` of them — is read."""
        offset = 0
        while True:
            size = self._page_size
            if spec.limit is not None:
                size = min(size, spec.limit - offset)
            page = await self.client.run_report(
                self._property, spec.request_body(offset=offset, page_size=size)
            )
            yield page
            offset += len(page.get("rows", ()))
            total = int(page.get("rowCount", 0))
            if not page.get("rows") or offset >= total or (spec.limit and offset >= spec.limit):
                return

    @staticmethod
    def _headers(page: dict[str, Any]) -> builtins.list[str]:
        return [h["name"] for h in page.get("dimensionHeaders", [])] + [
            h["name"] for h in page.get("metricHeaders", [])
        ]

    @staticmethod
    def _rows(page: dict[str, Any]) -> builtins.list[builtins.list[str]]:
        return [
            [v.get("value", "") for v in row.get("dimensionValues", [])]
            + [v.get("value", "") for v in row.get("metricValues", [])]
            for row in page.get("rows", [])
        ]

    async def run_report(self, spec: ReportSpec | dict[str, Any]) -> ReportResult:
        """Run `spec` and return all of its rows (up to `spec.limit`)."""
        if not isinstance(spec, ReportSpec):
            spec = ReportSpec.model_validate(spec)
        headers: list[str] = []
        metric_types: list[str] = []
        rows: list[list[str]] = []
        row_count = 0
        async for page in self._pages(spec):
            if not headers:
                headers = self._headers(page)
                metric_types = [h.get("type", "") for h in page.get("metricHeaders", [])]
                row_count = int(page.get("rowCount", 0))
            rows.extend(self._rows(page))
        return ReportResult(
            headers=headers, metric_types=metric_types, rows=rows, row_count=row_count
        )

    def _to_item(self, name: str, spec: ReportSpec) -> Item:
        return Item(
            id=name,
            name=f"{name}.csv",
            type=ItemType.FILE,
            path=f"{name}.csv",
            mime_type=CSV_MIME,
            metadata={
                "property": self._property,
                "dimensions": spec.dimensions,
                "metrics": spec.metrics,
                "date_ranges": [r.model_dump() for r in spec.date_ranges],
            },
        )

    async def list(
        self, path: str | None = None, *, recursive: bool = False
    ) -> AsyncIterator[Item]:
        if relative_path(path):
            raise NotFoundError(f"google_analytics has no folder {path!r}")
        for name, spec in self._reports.items():
            yield self._to_item(name, spec)

    async def get_metadata(self, item_id: str) -> Item:
        spec = self._reports.get(item_id)
        if spec is None:
            raise NotFoundError(f"google_analytics has no configured report {item_id!r}")
        return self._to_item(item_id, spec)

    async def download(self, item: Item) -> AsyncIterator[bytes]:
        # Only the configured report named by `item.id` runs — never what `item` claims.
        spec = self._reports.get(item.id)
        if spec is None:
            raise NotFoundError(f"google_analytics has no configured report {item.id!r}")
        first = True
        async for page in self._pages(spec):
            lines = self._rows(page)
            if first:
                lines = [self._headers(page), *lines]
                first = False
            yield to_csv(lines)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
