"""Google Analytics 4 reports as the Data API returns them: the properties a user can see (Admin
API), a property's fields (`metadata`), and `runReport` pages with their headers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from data_sources.core.logging import logger
from data_sources.reports.base import Account
from data_sources.reports.errors import Problem, ReportError
from data_sources.reports.google import GoogleReportSource, refresh_token_of
from data_sources.reports.http import ApiClient, Retry, Verdict, exponential
from data_sources.reports.models import RawReport, parse_day
from data_sources.reports.oauth import is_google_token_request
from data_sources.reports.paging import token_pages

SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
DATA_URL = "https://analyticsdata.googleapis.com/v1beta"
ADMIN_URL = "https://analyticsadmin.googleapis.com/v1beta"
#: Rows asked for per report request; the API allows up to 250,000.
PAGE_SIZE = 100_000
MAX_PAGES = 10_000
_PROPERTY = re.compile(r"^properties/\d+$")

UNAVAILABLE = Problem(
    "unavailable", "Google Analytics could not be reached or refused the request."
)
BUSY = Problem("unavailable", "Google Analytics kept throttling the request (quota exhausted?).")
INVALID = Problem("unavailable", "Google Analytics returned an invalid response.")
AUTH_FAILED = Problem("auth_failed", "Google refused the source's credentials; authorize again.")

_logger = logger.getChild("google_analytics.reports")


def classify(response: httpx.Response) -> Verdict:
    """429 is also how an exhausted property quota shows; Google may say how long to wait."""
    if response.status_code == 429 or response.status_code >= 500:
        try:
            after: float | None = float(response.headers["Retry-After"])
        except (KeyError, ValueError):
            after = None
        return Retry(BUSY, min(after, 60) if after is not None else None)
    if response.status_code in (401, 403) or (
        is_google_token_request(response) and response.is_error
    ):
        return AUTH_FAILED
    if response.is_error:
        return UNAVAILABLE
    return None


@dataclass(frozen=True)
class PropertySummary:
    """A GA4 property the user can see, with the account it belongs to."""

    property: str  # `properties/{id}`
    display_name: str
    account: str  # `accounts/{id}`
    account_name: str


@dataclass(frozen=True)
class FieldInfo:
    """A dimension or metric a property offers (from Google's `metadata`), custom ones
    included."""

    kind: str  # "dimension" or "metric"
    api_name: str
    display_name: str
    description: str
    category: str
    #: Defined on this property (`customEvent:...`, `customUser:...`, `customItem:...`).
    custom: bool
    #: Metrics only: `TYPE_INTEGER`, `TYPE_FLOAT`, `TYPE_SECONDS`, ...
    type: str | None = None


def parse_fields(metadata: dict[str, Any]) -> list[FieldInfo]:
    """The fields of a `properties/{id}/metadata` response, dimensions first."""
    fields = []
    for kind, key in (("dimension", "dimensions"), ("metric", "metrics")):
        for item in metadata.get(key, []):
            fields.append(
                FieldInfo(
                    kind=kind,
                    api_name=item["apiName"],
                    display_name=item.get("uiName", ""),
                    description=item.get("description", ""),
                    category=item.get("category", ""),
                    custom=bool(item.get("customDefinition")),
                    type=item.get("type") if kind == "metric" else None,
                )
            )
    return fields


def check_property(value: object) -> str:
    if not isinstance(value, str) or not _PROPERTY.match(value):
        raise ReportError("unavailable", "The source is not bound to a GA4 property.")
    return value


class GoogleAnalyticsReports(GoogleReportSource):
    """GA4 (`provider: google_analytics`). An account is a property (`properties/{id}`); a
    report spec is `{"dimensions", "metrics", "day_column"?}` (`day_column` defaults to
    `date`)."""

    provider = "google_analytics"
    scope = SCOPE
    token_rejected = AUTH_FAILED

    def _client(self) -> ApiClient:
        return ApiClient(
            classify,
            unreachable=UNAVAILABLE,
            invalid=INVALID,
            timeout=120,
            attempts=6,
            backoff=exponential(1.0),
            transport=self.transport,
            sleep=self.sleep,
        )

    async def list_accounts(self, credentials: Mapping[str, Any]) -> list[Account]:
        """The GA4 properties the user can see, grouped by their GA account."""
        return [
            Account(
                id=p.property,
                name=p.display_name,
                group=p.account_name,
                details={"account": p.account},
            )
            for p in await self.list_properties(refresh_token_of(credentials))
        ]

    async def read(
        self,
        credentials: Mapping[str, Any],
        account: str,
        spec: Mapping[str, Any],
        since: date,
        until: date,
    ) -> RawReport:
        return await self.read_report(
            refresh_token_of(credentials),
            account,
            dimensions=list(spec["dimensions"]),
            metrics=list(spec["metrics"]),
            since=since,
            until=until,
            day_column=spec.get("day_column", "date"),
        )

    async def list_properties(self, refresh_token: str) -> list[PropertySummary]:
        """Every GA4 property the user can see (Admin API `accountSummaries`; the Cloud project
        needs the Google Analytics Admin API enabled)."""
        async with self._client() as api:
            headers = await self.headers(api, refresh_token)

            async def fetch(token: str | None) -> dict[str, Any]:
                params: dict[str, Any] = {"pageSize": 200}
                if token:
                    params["pageToken"] = token
                return await api.json(
                    "GET", f"{ADMIN_URL}/accountSummaries", headers=headers, params=params
                )

            return [
                PropertySummary(
                    property=prop["property"],
                    display_name=prop.get("displayName", ""),
                    account=account["account"],
                    account_name=account.get("displayName", ""),
                )
                async for page in token_pages(fetch, invalid=INVALID)
                for account in page.get("accountSummaries", [])
                for prop in account.get("propertySummaries", [])
            ]

    async def list_fields(self, refresh_token: str, property_name: str) -> list[FieldInfo]:
        """Every dimension and metric the property offers, its custom ones included."""
        url = f"{DATA_URL}/{check_property(property_name)}/metadata"
        async with self._client() as api:
            headers = await self.headers(api, refresh_token)
            return parse_fields(await api.json("GET", url, headers=headers))

    async def read_report(
        self,
        refresh_token: str,
        property_name: str,
        *,
        dimensions: list[str],
        metrics: list[str],
        since: date,
        until: date,
        day_column: str | None = "date",
    ) -> RawReport:
        """A report over `since..until` (inclusive): each record is `runReport`'s
        `{"dimensionValues", "metricValues"}`, the envelope the first page without its rows
        (the headers say which value is which). `days` come from the `day_column` dimension."""
        url = f"{DATA_URL}/{check_property(property_name)}:runReport"
        body = {
            "dimensions": [{"name": name} for name in dimensions],
            "metrics": [{"name": name} for name in metrics],
            "dateRanges": [{"startDate": since.isoformat(), "endDate": until.isoformat()}],
            "limit": PAGE_SIZE,
        }
        records: list[dict[str, Any]] = []
        days: list[date | None] = []
        envelope: dict[str, Any] = {}
        at: int | None = None
        async with self._client() as api:
            headers = await self.headers(api, refresh_token)
            offset = 0
            for _ in range(MAX_PAGES):
                page = await api.json(
                    "POST", url, retry=True, headers=headers, json={**body, "offset": offset}
                )
                if not envelope:
                    envelope = {key: value for key, value in page.items() if key != "rows"}
                    names = [h["name"] for h in page.get("dimensionHeaders", [])]
                    at = names.index(day_column) if day_column in names else None
                rows = page.get("rows", [])
                for row in rows:
                    records.append(row)
                    value = row["dimensionValues"][at]["value"] if at is not None else None
                    days.append(parse_day(value) if value else None)
                offset += len(rows)
                # Done on an empty page, or once the rows Google says the report has are read.
                if not rows or offset >= int(page.get("rowCount", 0) or 0):
                    break
            else:
                raise INVALID.error()
        _logger.info(f"Read {len(records)} rows for {property_name}")
        return RawReport(records, days, envelope)
