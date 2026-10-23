from __future__ import annotations

import builtins
import json
import re
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

from pydantic import ValidationError

from data_sources.config.schema import ConnectorConfig
from data_sources.connectors.meta_ads.client import MetaAdsClient
from data_sources.connectors.meta_ads.models import (
    AdAccountSummary,
    BusinessSummary,
    InsightsResult,
    InsightsSpec,
)
from data_sources.connectors.meta_ads.oauth import DEFAULT_API_VERSION
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
from data_sources.ingest.source import ReportTable

#: Rows asked for per request.
DEFAULT_PAGE_SIZE = 500

CSV_MIME = "text/csv"

_ACCOUNT = re.compile(r"^(?:act_)?(\d+)$")
_VERSION = re.compile(r"^v\d+\.\d+$")


def parse_ad_account(value: str) -> str:
    """`act_{id}` from a bare numeric id or that form — `ConfigurationError` otherwise."""
    match = _ACCOUNT.match(value.strip())
    if match is None:
        raise ConfigurationError(
            f"meta_ads options.ad_account must be an ad account id such as '123456789' "
            f"or 'act_123456789', got {value!r}"
        )
    return f"act_{match.group(1)}"


def _report_specs(raw: Any) -> dict[str, InsightsSpec]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigurationError("meta_ads options.reports must map a name to a report")
    specs: dict[str, InsightsSpec] = {}
    for name, definition in raw.items():
        if not name or "/" in name:
            raise ConfigurationError(f"meta_ads report name {name!r} is not usable")
        try:
            specs[str(name)] = InsightsSpec.model_validate(definition)
        except ValidationError as exc:
            raise ConfigurationError(f"meta_ads report {name!r} is invalid: {exc}") from exc
    return specs


def _cell(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, builtins.list | dict):
        return json.dumps(value, separators=(",", ":"))
    return "" if value is None else str(value)


def _table(
    spec: InsightsSpec, records: builtins.list[dict[str, Any]]
) -> tuple[builtins.list[str], builtins.list[builtins.list[str]]]:
    """Header and rows for `records`: the requested fields first, then any other key the API
    returned, in the order first seen."""
    headers = list(spec.fields)
    for record in records:
        headers.extend(key for key in record if key not in headers)
    return headers, [[_cell(record.get(h)) for h in headers] for record in records]


@register_connector("meta_ads")
class MetaAdsConnector(Connector):
    """Connector for one Meta (Facebook / Instagram) ad account, read through the Marketing
    API's Insights.

    Like Google Analytics, ads data is report tables, not files, so it offers both views:

    - `run_insights(spec)` runs any query ad hoc and returns its rows (`InsightsResult`).
    - `options.reports` names queries (`{name: InsightsSpec-shaped dict}`); each is exposed
      as the file `<name>.csv` — `list()` yields them and `download` runs the query and
      streams the table as CSV, page by page.

    Addressed via `options.ad_account` (`act_123` or `123`). It may be left out to connect
    first and choose after: `list_ad_accounts()` lists the ad accounts the token can see
    (`list_businesses()` and `list_ad_accounts(business)` group them by business portfolio),
    and until one is set only `validate()` and those listings work.

    Auth is `type="access_token"` with `credentials.access_token` — a system-user token, or
    a long-lived user token from the consent flow (see `oauth`) — and optionally
    `credentials.app_secret`, which adds `appsecret_proof` to every call. Meta has no refresh
    tokens: a user token lasts about 60 days, after which the user consents again, and an
    expired token surfaces as `AuthenticationError`. `options.api_version` (default
    `oauth.DEFAULT_API_VERSION`) pins the Graph API version; `options.page_size` sets rows
    per request.

    The listing is flat: a path other than the root is refused (`NotFoundError`). Read-only —
    no sync, webhooks or permissions. Queries run synchronously, which Meta may time out for
    very large ranges; narrow the period or the level.
    """

    provider = "meta_ads"

    #: Attribution can move a day's conversions for up to 28 days after it.
    revision_days = 28

    def __init__(self, config: ConnectorConfig) -> None:
        super().__init__(config)
        account = config.options.get("ad_account")
        self._configured_account = parse_ad_account(str(account)) if account else None
        self._reports = _report_specs(config.options.get("reports"))
        self._api_version = str(config.options.get("api_version", DEFAULT_API_VERSION))
        if not _VERSION.match(self._api_version):
            raise ConfigurationError(
                f"meta_ads options.api_version must look like 'v26.0', got {self._api_version!r}"
            )
        self._page_size = int(config.options.get("page_size", DEFAULT_PAGE_SIZE))
        if not 1 <= self._page_size <= 5000:
            raise ConfigurationError("meta_ads options.page_size must be 1 to 5000")
        self._client: MetaAdsClient | None = None
        self.logger = logger.getChild("meta_ads.connector")

    @property
    def client(self) -> MetaAdsClient:
        if self._client is None:
            raise ConnectionError(
                f"{self.provider} connector is not connected; call connect() first"
            )
        return self._client

    @property
    def _account(self) -> str:
        if self._configured_account is None:
            raise ConfigurationError(
                "meta_ads connector has no options.ad_account; pick one from "
                "list_ad_accounts() first"
            )
        return self._configured_account

    async def connect(self) -> None:
        auth = self.config.auth
        if auth is None:
            raise ConfigurationError("meta_ads connector requires auth")
        if auth.type != "access_token":
            raise ConfigurationError(
                f"meta_ads connector auth.type must be 'access_token', got {auth.type!r}"
            )
        token = auth.credentials.get("access_token")
        if not token:
            raise ConfigurationError("meta_ads connector auth.credentials missing: access_token")
        self._client = MetaAdsClient(
            str(token),
            app_secret=auth.credentials.get("app_secret"),
            api_version=self._api_version,
        )

    async def validate(self) -> bool:
        try:
            if self._configured_account is None:
                await self.client.get("me/adaccounts", {"fields": "id", "limit": 1})
            else:
                await self.client.get(self._configured_account, {"fields": "id"})
        except DataSourceError as exc:
            self.logger.warning(f"validate() failed for {self._configured_account}: {exc}")
            return False
        return True

    async def list_businesses(self) -> builtins.list[BusinessSummary]:
        """The business portfolios the token's user belongs to. Needs the
        `business_management` permission, or `AuthenticationError` says it's missing."""
        businesses: builtins.list[BusinessSummary] = []
        async for page in self.client.get_pages(
            "me/businesses", {"fields": "id,name", "limit": 100}
        ):
            businesses.extend(
                BusinessSummary(id=raw["id"], name=raw.get("name", "")) for raw in page
            )
        return businesses

    async def list_ad_accounts(
        self, business: str | None = None
    ) -> builtins.list[AdAccountSummary]:
        """The ad accounts the token can see (`ads_read` covers it) — or, given a `business`
        id from `list_businesses()`, those that portfolio owns and those shared in to it as a
        client (needs `business_management`), each marked with how it holds it."""
        if business is None:
            return await self._ad_accounts("me/adaccounts")
        if not business.isdigit():
            raise ValueError(f"{business!r} is not a business id")
        accounts: dict[str, AdAccountSummary] = {}
        for relationship in ("owned", "client"):
            for account in await self._ad_accounts(
                f"{business}/{relationship}_ad_accounts", business, relationship
            ):
                accounts.setdefault(account.account, account)
        return builtins.list(accounts.values())

    async def _ad_accounts(
        self, edge: str, business: str | None = None, relationship: str | None = None
    ) -> builtins.list[AdAccountSummary]:
        accounts: builtins.list[AdAccountSummary] = []
        params = {"fields": "id,name,currency,account_status,timezone_name", "limit": 100}
        async for page in self.client.get_pages(edge, params):
            accounts.extend(
                AdAccountSummary.model_validate(
                    {
                        "account": raw["id"],
                        "name": raw.get("name", ""),
                        "currency": raw.get("currency", ""),
                        "status": int(raw.get("account_status", 0)),
                        "timezone": raw.get("timezone_name", ""),
                        "business_id": business,
                        "relationship": relationship,
                    }
                )
                for raw in page
            )
        return accounts

    async def _pages(self, spec: InsightsSpec) -> AsyncIterator[builtins.list[dict[str, Any]]]:
        """The query's pages of records, until every row — or `spec.limit` of them — is read."""
        after: str | None = None
        fetched = 0
        while True:
            size = self._page_size
            if spec.limit is not None:
                size = min(size, spec.limit - fetched)
            page = await self.client.get(
                f"{self._account}/insights", spec.request_params(page_size=size, after=after)
            )
            records: builtins.list[dict[str, Any]] = page.get("data", [])
            yield records
            fetched += len(records)
            paging = page.get("paging", {})
            after = paging.get("cursors", {}).get("after")
            if not records or "next" not in paging or not after:
                return
            if spec.limit is not None and fetched >= spec.limit:
                return

    async def run_insights(self, spec: InsightsSpec | dict[str, Any]) -> InsightsResult:
        """Run `spec` and return all of its rows (up to `spec.limit`)."""
        if not isinstance(spec, InsightsSpec):
            spec = InsightsSpec.model_validate(spec)
        records = [record async for page in self._pages(spec) for record in page]
        headers, rows = _table(spec, records)
        return InsightsResult(headers=headers, rows=rows)

    def day_column(self, spec: InsightsSpec) -> str | None:
        """`date_start` when the query is split by single days (`time_increment=1`), else
        `None` — what `ingest.ReportLoader` keys its incremental loads on."""
        return "date_start" if spec.time_increment == 1 else None

    async def read_table(
        self, spec: InsightsSpec, since: date | None, until: date | None
    ) -> ReportTable:
        """`spec`'s rows over `since`..`until` (its own period when `None`), as the loader
        takes them (see `ingest.TabularSource`)."""
        if since is not None and until is not None:
            spec = spec.model_copy(
                update={
                    "since": since.isoformat(),
                    "until": until.isoformat(),
                    "date_preset": None,
                }
            )
        result = await self.run_insights(spec)
        return ReportTable(
            headers=result.headers, rows=result.rows, day_column=self.day_column(spec)
        )

    def _to_item(self, name: str, spec: InsightsSpec) -> Item:
        return Item(
            id=name,
            name=f"{name}.csv",
            type=ItemType.FILE,
            path=f"{name}.csv",
            mime_type=CSV_MIME,
            metadata={
                "ad_account": self._account,
                "fields": spec.fields,
                "level": spec.level,
                "date_preset": spec.date_preset,
                "since": spec.since,
                "until": spec.until,
            },
        )

    async def list(
        self, path: str | None = None, *, recursive: bool = False
    ) -> AsyncIterator[Item]:
        if relative_path(path):
            raise NotFoundError(f"meta_ads has no folder {path!r}")
        for name, spec in self._reports.items():
            yield self._to_item(name, spec)

    async def get_metadata(self, item_id: str) -> Item:
        spec = self._reports.get(item_id)
        if spec is None:
            raise NotFoundError(f"meta_ads has no configured report {item_id!r}")
        return self._to_item(item_id, spec)

    async def download(self, item: Item) -> AsyncIterator[bytes]:
        # Only the configured report named by `item.id` runs — never what `item` claims.
        spec = self._reports.get(item.id)
        if spec is None:
            raise NotFoundError(f"meta_ads has no configured report {item.id!r}")
        header_written = False
        seen: builtins.list[str] = []
        async for records in self._pages(spec):
            headers, rows = _table(spec, records)
            if not header_written:
                seen = headers
                header_written = True
                yield to_csv([headers, *rows])
            else:
                # A later page can carry a key the first didn't; CSV columns stay as the
                # first page's, so such cells are dropped rather than shifting columns.
                yield to_csv([[_cell(r.get(h)) for h in seen] for r in records])

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
