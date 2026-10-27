"""Google Search Console: the sites a user can read and daily Search Analytics reports. Rows stay
as the API returned them."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import quote

import httpx

from data_sources.core.logging import logger
from data_sources.reports.base import Account
from data_sources.reports.errors import Problem, ReportError
from data_sources.reports.google import GoogleReportSource, refresh_token_of
from data_sources.reports.http import ApiClient, Retry, Verdict, linear
from data_sources.reports.models import RawReport, parse_day
from data_sources.reports.oauth import is_google_token_request
from data_sources.reports.windows import each_day

SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
API_URL = "https://www.googleapis.com/webmasters/v3"
PAGE_SIZE = 25_000
DAILY_LIMIT = 50_000
METRICS = ("clicks", "impressions", "ctr", "position")
READ_PERMISSIONS = {"siteOwner", "siteFullUser", "siteRestrictedUser"}
RATE_LIMIT_REASONS = {
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "quotaExceeded",
    "dailyLimitExceeded",
}

UNREACHABLE = Problem("unavailable", "Search Console could not be reached.")
BUSY = Problem("unavailable", "Search Console is unavailable or rate limited.")
INVALID = Problem("unavailable", "Search Console returned invalid JSON.")
AUTH_FAILED = Problem("auth_failed", "Reconnect and check Search Console access.")
REFUSED = Problem("request_failed", "Search Console refused the request.")

_logger = logger.getChild("search_console.client")


def _fail(message: str, reason: str = "unavailable") -> ReportError:
    return Problem(reason, message).error()


def _rate_limited(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return False
    errors = error.get("errors", [])
    return error.get("status") == "RESOURCE_EXHAUSTED" or (
        isinstance(errors, list)
        and any(isinstance(e, dict) and e.get("reason") in RATE_LIMIT_REASONS for e in errors)
    )


def classify(response: httpx.Response) -> Verdict:
    if _rate_limited(response) or response.status_code >= 500:
        return Retry(BUSY)
    if response.status_code in (401, 403) or (
        is_google_token_request(response) and response.is_error
    ):
        return AUTH_FAILED
    if response.is_error:
        return REFUSED
    return None


@dataclass(frozen=True)
class SearchConsoleSite:
    site_url: str
    permission_level: str


class SearchConsoleReports(GoogleReportSource):
    """Search Console (`provider: google_search_console`). An account is a site URL; a report
    spec is `{"dimensions"}`, the first of which must be `date`."""

    provider = "google_search_console"
    scope = SCOPE

    def _client(self, timeout: float = 60) -> ApiClient:
        return ApiClient(
            classify,
            unreachable=UNREACHABLE,
            invalid=INVALID,
            timeout=timeout,
            backoff=linear(),
            transport=self.transport,
            sleep=self.sleep,
        )

    async def list_accounts(self, credentials: Mapping[str, Any]) -> list[Account]:
        """The sites the user can read, with their permission level."""
        return [
            Account(
                id=s.site_url, name=s.site_url, details={"permission_level": s.permission_level}
            )
            for s in await self.list_sites(refresh_token_of(credentials))
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
            refresh_token_of(credentials), account, list(spec["dimensions"]), since, until
        )

    async def list_sites(self, refresh_token: str) -> list[SearchConsoleSite]:
        """The sites the user can read (owners, full and restricted users)."""
        async with self._client(timeout=30) as api:
            headers = await self.headers(api, refresh_token)
            body = await api.json("GET", f"{API_URL}/sites", headers=headers)
        entries = body.get("siteEntry", [])
        if not isinstance(entries, list) or any(
            not isinstance(s, dict)
            or not isinstance(s.get("siteUrl"), str)
            or not s["siteUrl"]
            or not isinstance(s.get("permissionLevel"), str)
            for s in entries
        ):
            raise _fail("Search Console returned invalid properties.")
        return [
            SearchConsoleSite(s["siteUrl"], s["permissionLevel"])
            for s in entries
            if s["permissionLevel"] in READ_PERMISSIONS
        ]

    async def read_report(
        self, refresh_token: str, site_url: str, dimensions: list[str], since: date, until: date
    ) -> RawReport:
        """Search Analytics (web, all data states) by `dimensions`, whose first must be `date`,
        one day at a time: a long range otherwise loses rows to the API's top-row cap. A day
        that reaches `DAILY_LIMIT` rows is listed in the envelope's `limitedDays`."""
        if since > until or not site_url or not dimensions or dimensions[0] != "date":
            raise _fail("Invalid Search Console report definition.", "request_failed")
        url = f"{API_URL}/sites/{quote(site_url, safe='')}/searchAnalytics/query"
        records: list[dict[str, Any]] = []
        days: list[date | None] = []
        limited: list[str] = []
        envelope: dict[str, Any] = {
            "siteUrl": site_url,
            "dimensions": dimensions,
            "searchType": "web",
            "dataState": "all",
            "limitedDays": limited,
        }
        async with self._client() as api:
            headers = await self.headers(api, refresh_token)
            for day in each_day(since, until):
                offset = 0
                while offset < DAILY_LIMIT:
                    body = await api.json(
                        "POST",
                        url,
                        retry=True,
                        headers=headers,
                        json={
                            "startDate": day.isoformat(),
                            "endDate": day.isoformat(),
                            "dimensions": dimensions,
                            "type": "web",
                            "dataState": "all",
                            "aggregationType": "byPage" if "page" in dimensions else "byProperty",
                            "rowLimit": PAGE_SIZE,
                            "startRow": offset,
                        },
                    )
                    rows = body.get("rows", [])
                    if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
                        raise _fail("Search Console returned invalid rows.")
                    for row in rows:
                        keys = row.get("keys") if isinstance(row, dict) else None
                        if (
                            not isinstance(keys, list)
                            or len(keys) != len(dimensions)
                            or not all(isinstance(k, str) for k in keys)
                            or parse_day(keys[0]) != day
                            or any(
                                type(row.get(m)) not in (int, float) or not math.isfinite(row[m])
                                for m in METRICS
                            )
                        ):
                            raise _fail("Search Console returned an invalid daily row.")
                    records.extend(rows)
                    days.extend([day] * len(rows))
                    offset += len(rows)
                    if len(rows) < PAGE_SIZE:
                        break
                if offset >= DAILY_LIMIT:
                    limited.append(day.isoformat())
        _logger.info(f"Read {len(records)} rows by {','.join(dimensions)} for {site_url}")
        return RawReport(records, days, envelope)
