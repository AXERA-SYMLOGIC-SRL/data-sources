"""Google Merchant Center (Merchant API): the accounts a user can read and daily product
performance reports. Rows stay as the API returned them (`{"productPerformanceView": {...}}`).

The Merchant API only answers a Google Cloud project registered with the merchant account's
Merchant Center (`developerRegistration`); until then every call is a 403, which is reported as an
authorization failure."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import date
from typing import Any

import httpx

from data_sources.core.logging import logger
from data_sources.reports.base import Account
from data_sources.reports.errors import Problem, ReportError
from data_sources.reports.google import GoogleReportSource, refresh_token_of
from data_sources.reports.http import ApiClient, Retry, Verdict, linear
from data_sources.reports.models import RawReport
from data_sources.reports.oauth import is_google_token_request
from data_sources.reports.windows import windows

SCOPE = "https://www.googleapis.com/auth/content"
ACCOUNTS_URL = "https://merchantapi.googleapis.com/accounts/v1/accounts"
REPORTS_URL = "https://merchantapi.googleapis.com/reports/v1/accounts/{account}/reports:search"
ACCOUNT_PAGE_SIZE = 500
REPORT_PAGE_SIZE = 10_000
#: Days per query: a long range would make one very long paged read.
WINDOW_DAYS = 31
#: The reports with a `date` segment: the table name in the query, and the key the API wraps a row
#: in.
VIEWS = {"product_performance_view": "productPerformanceView"}
FIELD = re.compile(r"^[a-z][a-z0-9_]*$")
ACCOUNT_ID = re.compile(r"^[0-9]+$")
RATE_LIMIT_STATUSES = {"RESOURCE_EXHAUSTED"}

UNREACHABLE = Problem("unavailable", "Merchant Center could not be reached.")
BUSY = Problem("unavailable", "Merchant Center is unavailable or rate limited.")
INVALID = Problem("unavailable", "Merchant Center returned invalid JSON.")
AUTH_FAILED = Problem(
    "auth_failed",
    "Reconnect and check Merchant Center access. "
    "The Google Cloud project must also be registered with the account.",
)
REFUSED = Problem("request_failed", "Merchant Center refused the request.")

_logger = logger.getChild("google_merchant.client")


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
    return isinstance(error, dict) and error.get("status") in RATE_LIMIT_STATUSES


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


def _timezone(value: object) -> str:
    """The account's time zone id: v1 sends `{"id": "Europe/Bucharest"}`, older shapes a string."""
    if isinstance(value, dict):
        value = value.get("id")
    return value if isinstance(value, str) else ""


def _number(value: object) -> bool:
    """A metric as the API sends it: an int64 as a string, or a finite JSON number."""
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        return value.lstrip("-").isdigit()
    return isinstance(value, (int, float)) and math.isfinite(value)


def _date_of(value: object) -> date | None:
    """The API's `{"year", "month", "day"}` date object."""
    if not isinstance(value, dict):
        return None
    try:
        return date(int(value["year"]), int(value["month"]), int(value["day"]))
    except (KeyError, TypeError, ValueError):
        return None


class GoogleMerchantReports(GoogleReportSource):
    """Merchant Center (`provider: google_merchant`). An account is a merchant id (digits); a
    report spec is `{"view": "product_performance_view", "fields": [...]}`, the fields including
    `date` (the API wants a metric beside any segment). Dates are in the account's time zone."""

    provider = "google_merchant"
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
        """The merchant accounts the user can access (an advanced account's sub-accounts too)."""
        found: list[Account] = []
        token = ""
        async with self._client(timeout=30) as api:
            headers = await self.headers(api, refresh_token_of(credentials))
            while True:
                params = {"pageSize": ACCOUNT_PAGE_SIZE, **({"pageToken": token} if token else {})}
                body = await api.json("GET", ACCOUNTS_URL, headers=headers, params=params)
                entries = body.get("accounts", [])
                if not isinstance(entries, list):
                    raise _fail("Merchant Center returned invalid accounts.")
                for entry in entries:
                    account_id = str(entry.get("accountId", "")) if isinstance(entry, dict) else ""
                    if not ACCOUNT_ID.match(account_id):
                        raise _fail("Merchant Center returned invalid accounts.")
                    name = entry.get("accountName")
                    found.append(
                        Account(
                            id=account_id,
                            name=name if isinstance(name, str) and name else account_id,
                            details={
                                "time_zone": _timezone(entry.get("timeZone")),
                                "language_code": entry.get("languageCode")
                                if isinstance(entry.get("languageCode"), str)
                                else "",
                                "test_account": entry.get("testAccount") is True,
                            },
                        )
                    )
                token = body.get("nextPageToken") or ""
                if not isinstance(token, str) or not token:
                    return found

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
            str(spec["view"]),
            list(spec["fields"]),
            since,
            until,
        )

    async def read_report(
        self,
        refresh_token: str,
        account: str,
        view: str,
        fields: list[str],
        since: date,
        until: date,
    ) -> RawReport:
        """`view` over `since..until` with `fields` (snake_case, as the query language takes them),
        in windows of `WINDOW_DAYS`, every page of each. A row whose date falls outside the window,
        or whose counts are not numbers, fails the read. The envelope names the account, view and
        fields."""
        if (
            since > until
            or not ACCOUNT_ID.match(account)
            or view not in VIEWS
            or "date" not in fields
            or len(set(fields)) != len(fields)
            or not all(isinstance(f, str) and FIELD.match(f) for f in fields)
        ):
            raise _fail("Invalid Merchant Center report definition.", "request_failed")
        key = VIEWS[view]
        url = REPORTS_URL.format(account=account)
        records: list[dict[str, Any]] = []
        days: list[date | None] = []
        envelope: dict[str, Any] = {"accountId": account, "view": view, "fields": fields}
        async with self._client() as api:
            headers = await self.headers(api, refresh_token)
            for start, end in windows(since, until, WINDOW_DAYS):
                query = (
                    f"SELECT {', '.join(fields)} FROM {view} "
                    f"WHERE date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"
                )
                token = ""
                while True:
                    payload: dict[str, Any] = {"query": query, "pageSize": REPORT_PAGE_SIZE}
                    if token:
                        payload["pageToken"] = token
                    body = await api.json("POST", url, retry=True, headers=headers, json=payload)
                    rows = body.get("results", [])
                    if not isinstance(rows, list):
                        raise _fail("Merchant Center returned invalid rows.")
                    for row in rows:
                        inner = row.get(key) if isinstance(row, dict) else None
                        if not isinstance(inner, dict):
                            raise _fail("Merchant Center returned an invalid daily row.")
                        day = _date_of(inner.get("date"))
                        counts = (inner[m] for m in ("clicks", "impressions") if m in inner)
                        if (
                            day is None
                            or not start <= day <= end
                            or not all(_number(c) for c in counts)
                        ):
                            raise _fail("Merchant Center returned an invalid daily row.")
                        records.append(row)
                        days.append(day)
                    token = body.get("nextPageToken") or ""
                    if not isinstance(token, str) or not token:
                        break
        _logger.info(f"Read {len(records)} rows of {view} for {account}")
        return RawReport(records, days, envelope)


__all__ = ["GoogleMerchantReports"]
