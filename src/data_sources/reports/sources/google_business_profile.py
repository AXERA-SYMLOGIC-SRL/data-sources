"""Google Business Profile: the locations a user manages, their daily performance metrics, the
keywords they were found with each month and their reviews.

Five APIs sit behind it (account management, business information, business profile performance
and the v4 reviews API), each of which must be enabled on the Google Cloud project, and Google only
gives the project quota once it approves its access request: until then calls are refused as rate
limited with a quota of zero."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
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

SCOPE = "https://www.googleapis.com/auth/business.manage"
ACCOUNTS_URL = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
LOCATIONS_URL = "https://mybusinessbusinessinformation.googleapis.com/v1/{account}/locations"
PERFORMANCE_URL = "https://businessprofileperformance.googleapis.com/v1/{location}"
REVIEWS_URL = "https://mybusiness.googleapis.com/v4/{location}/reviews"
ACCOUNT_PAGE_SIZE = 20
LOCATION_PAGE_SIZE = 100
KEYWORD_PAGE_SIZE = 100
REVIEW_PAGE_SIZE = 50
#: Days per metrics request.
WINDOW_DAYS = 31
#: The daily metrics Google still serves (its conversation and food order metrics are deprecated).
DAILY_METRICS = (
    "BUSINESS_IMPRESSIONS_DESKTOP_MAPS",
    "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH",
    "BUSINESS_IMPRESSIONS_MOBILE_MAPS",
    "BUSINESS_IMPRESSIONS_MOBILE_SEARCH",
    "BUSINESS_DIRECTION_REQUESTS",
    "CALL_CLICKS",
    "WEBSITE_CLICKS",
    "MESSAGING_CLICKS",
    "BUSINESS_BOOKINGS",
    "BUSINESS_FOOD_MENU_CLICKS",
)
REPORTS = ("daily_metrics", "search_keywords", "reviews")
#: A bound location: `accounts/{account}/locations/{location}`.
LOCATION = re.compile(r"^accounts/[0-9]+/locations/[0-9]+$")
RATE_LIMIT_STATUSES = {"RESOURCE_EXHAUSTED"}

UNREACHABLE = Problem("unavailable", "Business Profile could not be reached.")
BUSY = Problem(
    "unavailable",
    "Business Profile is unavailable, rate limited, or the Google Cloud project has no API quota "
    "yet (Google must approve its Business Profile API access).",
)
INVALID = Problem("unavailable", "Business Profile returned invalid JSON.")
AUTH_FAILED = Problem(
    "auth_failed",
    "Reconnect and check Business Profile access. The Business Profile APIs must also be enabled "
    "on the Google Cloud project.",
)
REFUSED = Problem("request_failed", "Business Profile refused the request.")

_logger = logger.getChild("google_business_profile.client")


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


def _today() -> date:
    return datetime.now(UTC).date()


def _date_of(value: object) -> date | None:
    """The API's `{"year", "month", "day"}` date object."""
    if not isinstance(value, dict):
        return None
    try:
        return date(int(value["year"]), int(value["month"]), int(value["day"]))
    except (KeyError, TypeError, ValueError):
        return None


def _count(value: object) -> bool:
    """An int64 as the API sends it: a string of digits."""
    return isinstance(value, str) and value.isdigit()


def _months(count: int, today: date) -> list[tuple[int, int]]:
    """The `count` complete months before `today`, oldest first, as `(year, month)`."""
    year, month = today.year, today.month
    found: list[tuple[int, int]] = []
    for _ in range(count):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        found.append((year, month))
    return found[::-1]


def _date_params(prefix: str, day: date) -> dict[str, int]:
    return {
        f"{prefix}.year": day.year,
        f"{prefix}.month": day.month,
        f"{prefix}.day": day.day,
    }


class GoogleBusinessProfileReports(GoogleReportSource):
    """Business Profile (`provider: google_business_profile`). An account is a location,
    `accounts/{account}/locations/{location}` (`group`: the account it belongs to). A report spec
    is `{"report": "daily_metrics"}` (a record per day: `{"date", "metrics": {metric: count}}`),
    `{"report": "search_keywords", "months": 12}` (a record per keyword and month, for the last
    complete months, whatever the window) or `{"report": "reviews"}` (the location's reviews as
    they are now). The last two have no day: they are read whole every time."""

    provider = "google_business_profile"
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
        """Every location of every account the user can access."""
        found: list[Account] = []
        async with self._client(timeout=30) as api:
            headers = await self.headers(api, refresh_token_of(credentials))
            accounts = await self._pages(
                api, ACCOUNTS_URL, headers, "accounts", {"pageSize": ACCOUNT_PAGE_SIZE}
            )
            for account in accounts:
                name = account.get("name") if isinstance(account, dict) else None
                if not isinstance(name, str) or not re.match(r"^accounts/[0-9]+$", name):
                    raise _fail("Business Profile returned invalid accounts.")
                label = account.get("accountName")
                locations = await self._pages(
                    api,
                    LOCATIONS_URL.format(account=name),
                    headers,
                    "locations",
                    {
                        "pageSize": LOCATION_PAGE_SIZE,
                        "readMask": "name,title,storefrontAddress",
                    },
                )
                for location in locations:
                    parts = location.get("name") if isinstance(location, dict) else None
                    if not isinstance(parts, str) or not re.match(r"^locations/[0-9]+$", parts):
                        raise _fail("Business Profile returned invalid locations.")
                    title = location.get("title")
                    address = location.get("storefrontAddress")
                    address = address if isinstance(address, dict) else {}
                    found.append(
                        Account(
                            id=f"{name}/{parts}",
                            name=title if isinstance(title, str) and title else parts,
                            group=label if isinstance(label, str) else "",
                            details={
                                "locality": str(address.get("locality") or ""),
                                "region_code": str(address.get("regionCode") or ""),
                            },
                        )
                    )
        return found

    async def _pages(
        self,
        api: ApiClient,
        url: str,
        headers: dict[str, str],
        key: str,
        params: Mapping[str, Any],
    ) -> list[Any]:
        """Every item under `key` over the pages of a list call."""
        items: list[Any] = []
        token = ""
        while True:
            query = {**params, **({"pageToken": token} if token else {})}
            body = await api.json("GET", url, headers=headers, params=query)
            page = body.get(key, [])
            if not isinstance(page, list):
                raise _fail(f"Business Profile returned invalid {key}.")
            items.extend(page)
            token = body.get("nextPageToken") or ""
            if not isinstance(token, str) or not token:
                return items

    async def read(
        self,
        credentials: Mapping[str, Any],
        account: str,
        spec: Mapping[str, Any],
        since: date,
        until: date,
    ) -> RawReport:
        refresh = refresh_token_of(credentials)
        report = spec.get("report")
        if report == "daily_metrics":
            return await self.read_daily_metrics(refresh, account, since, until)
        if report == "search_keywords":
            return await self.read_search_keywords(refresh, account, int(spec.get("months", 12)))
        if report == "reviews":
            return await self.read_reviews(refresh, account)
        raise _fail("Unsupported Business Profile report.", "request_failed")

    def _location(self, account: str) -> str:
        if not LOCATION.match(account):
            raise _fail("Invalid Business Profile location.", "request_failed")
        return account.split("/", 2)[2]

    async def read_daily_metrics(
        self, refresh_token: str, account: str, since: date, until: date
    ) -> RawReport:
        """A record per day Google listed for any metric, `{"date", "metrics": {metric: count}}`:
        a metric Google listed for that day without a value is 0 (it leaves zeros out), one it
        did not send at all is absent. Days Google has no data for yet are not in the report."""
        location = self._location(account)
        if since > until:
            raise _fail("Invalid Business Profile report definition.", "request_failed")
        per_day: dict[date, dict[str, str]] = {}
        async with self._client() as api:
            headers = await self.headers(api, refresh_token)
            for start, end in windows(since, until, WINDOW_DAYS):
                params: list[tuple[str, Any]] = [("dailyMetrics", m) for m in DAILY_METRICS]
                params += list(_date_params("dailyRange.start_date", start).items())
                params += list(_date_params("dailyRange.end_date", end).items())
                url = (
                    PERFORMANCE_URL.format(location=location) + ":fetchMultiDailyMetricsTimeSeries"
                )
                body = await api.json("GET", url, headers=headers, params=params)
                groups = body.get("multiDailyMetricTimeSeries", [])
                if not isinstance(groups, list):
                    raise _fail("Business Profile returned invalid metrics.")
                for group in groups:
                    series = group.get("dailyMetricTimeSeries") if isinstance(group, dict) else None
                    if not isinstance(series, list):
                        raise _fail("Business Profile returned invalid metrics.")
                    for entry in series:
                        self._add_series(entry, start, end, per_day)
        days = sorted(per_day)
        _logger.info(f"Read {len(days)} days of metrics for {account}")
        return RawReport(
            [{"date": _date_object(d), "metrics": per_day[d]} for d in days],
            list(days),
            {"location": account, "metrics": list(DAILY_METRICS)},
        )

    def _add_series(
        self, entry: object, start: date, end: date, per_day: dict[date, dict[str, str]]
    ) -> None:
        metric = entry.get("dailyMetric") if isinstance(entry, dict) else None
        values = (
            (entry.get("timeSeries") or {}).get("datedValues", [])
            if isinstance(entry, dict)
            else None
        )
        if metric not in DAILY_METRICS or not isinstance(values, list):
            raise _fail("Business Profile returned invalid metrics.")
        for point in values:
            day = _date_of(point.get("date")) if isinstance(point, dict) else None
            value = point.get("value", "0") if isinstance(point, dict) else None
            if (
                day is None
                or not start <= day <= end
                or not isinstance(value, str)
                or not _count(value)
            ):
                raise _fail("Business Profile returned an invalid daily value.")
            per_day.setdefault(day, {})[metric] = value

    async def read_search_keywords(
        self, refresh_token: str, account: str, months: int
    ) -> RawReport:
        """A record per keyword and month for the last `months` complete months, oldest first:
        `{"month": {"year", "month"}, "searchKeyword", "insightsValue": {"value"|"threshold"}}`.
        Google sums a range, so each month is its own request; a `threshold` means "fewer than"."""
        location = self._location(account)
        if not 1 <= months <= 18:
            raise _fail("Invalid Business Profile report definition.", "request_failed")
        chosen = _months(months, _today())
        records: list[dict[str, Any]] = []
        url = PERFORMANCE_URL.format(location=location) + "/searchkeywords/impressions/monthly"
        async with self._client() as api:
            headers = await self.headers(api, refresh_token)
            for year, month in chosen:
                params = {
                    "monthlyRange.start_month.year": year,
                    "monthlyRange.start_month.month": month,
                    "monthlyRange.end_month.year": year,
                    "monthlyRange.end_month.month": month,
                    "pageSize": KEYWORD_PAGE_SIZE,
                }
                for item in await self._pages(api, url, headers, "searchKeywordsCounts", params):
                    value = item.get("insightsValue") if isinstance(item, dict) else None
                    count = (value or {}).get("value", (value or {}).get("threshold"))
                    if not isinstance(item.get("searchKeyword"), str) or not _count(count):
                        raise _fail("Business Profile returned an invalid keyword.")
                    records.append(
                        {
                            "month": {"year": year, "month": month},
                            "searchKeyword": item["searchKeyword"],
                            "insightsValue": value,
                        }
                    )
        envelope = {"location": account, "months": [f"{y}-{m:02d}" for y, m in chosen]}
        return RawReport(records, [None] * len(records), envelope)

    async def read_reviews(self, refresh_token: str, account: str) -> RawReport:
        """The location's reviews as they are now, each as the API returned it. The envelope
        carries the location's `averageRating` and `totalReviewCount`."""
        self._location(account)
        url = REVIEWS_URL.format(location=account)
        records: list[dict[str, Any]] = []
        envelope: dict[str, Any] = {"location": account}
        token = ""
        async with self._client() as api:
            headers = await self.headers(api, refresh_token)
            while True:
                params: dict[str, Any] = {
                    "pageSize": REVIEW_PAGE_SIZE,
                    "orderBy": "updateTime desc",
                }
                if token:
                    params["pageToken"] = token
                body = await api.json("GET", url, headers=headers, params=params)
                page = body.get("reviews", [])
                if not isinstance(page, list) or not all(
                    isinstance(r, dict) and isinstance(r.get("reviewId"), str) for r in page
                ):
                    raise _fail("Business Profile returned invalid reviews.")
                records.extend(page)
                for key in ("averageRating", "totalReviewCount"):
                    if key in body:
                        envelope[key] = body[key]
                token = body.get("nextPageToken") or ""
                if not isinstance(token, str) or not token:
                    break
        _logger.info(f"Read {len(records)} reviews for {account}")
        return RawReport(records, [None] * len(records), envelope)


def _date_object(day: date) -> dict[str, int]:
    return {"year": day.year, "month": day.month, "day": day.day}


__all__ = ["GoogleBusinessProfileReports"]
