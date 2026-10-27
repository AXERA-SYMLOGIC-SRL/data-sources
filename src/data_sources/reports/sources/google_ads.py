"""Google Ads, read-only, through the REST `googleAds:search` endpoint (GAQL): the customers a user
can reach (directly or through a manager) and daily reports. Credentials never appear in URLs or
errors; refused requests are logged with them removed (`google_ads_errors`)."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from data_sources.core.logging import logger
from data_sources.reports.base import Account
from data_sources.reports.errors import Problem, ReportError
from data_sources.reports.google import GoogleReportSource, refresh_token_of
from data_sources.reports.google_ads_errors import (
    google_ads_error_codes,
    google_ads_error_summary,
    log_google_ads_error,
)
from data_sources.reports.http import ApiClient, Classify, Retry, Verdict
from data_sources.reports.models import RawReport, parse_day
from data_sources.reports.oauth import is_google_token_request
from data_sources.reports.paging import token_pages
from data_sources.reports.windows import windows

SCOPE = "https://www.googleapis.com/auth/adwords"
API_VERSION = "v26"
REPORT_WINDOW_DAYS = 30

UNREACHABLE = Problem("unavailable", "Google Ads could not be reached.")
BUSY = Problem("unavailable", "Google Ads is temporarily unavailable or rate limited.")
INVALID = Problem("unavailable", "Google Ads returned invalid JSON.")
NOT_ENABLED = Problem(
    "customer_not_enabled",
    "The Google Ads customer account is not enabled or has been deactivated.",
)
AUTH_FAILED = Problem(
    "auth_failed",
    "Check Google Ads consent, account access and Cloud project API approval.",
)
REFUSED_MESSAGE = "Google Ads refused the request; check app configuration and account access."
_FIELD = re.compile(r"[a-z_]+(\.[a-z_]+)*")

_logger = logger.getChild("google_ads.client")


def _fail(message: str, reason: str = "unavailable") -> ReportError:
    return Problem(reason, message).error()


def make_classify(
    log: Callable[[httpx.Response], None],
    summarize: Callable[[httpx.Response], str] = lambda _: "",
) -> Classify:
    def classify(response: httpx.Response) -> Verdict:
        if response.is_error:
            log(response)
        if response.status_code == 429 or response.status_code >= 500:
            return Retry(BUSY)
        if response.status_code == 403 and google_ads_error_codes(response) == {
            "CUSTOMER_NOT_ENABLED"
        }:
            return NOT_ENABLED
        if response.status_code in {401, 403}:
            return AUTH_FAILED
        if response.is_error:
            # Provider diagnostics were logged with redaction; errors stay stable.
            if is_google_token_request(response):
                return Problem("auth_failed", REFUSED_MESSAGE)
            # Google's own error codes and messages (no credentials) travel apart from the stable
            # message, so an application can show them where an admin looks.
            return Problem("request_failed", REFUSED_MESSAGE, summarize(response))
        return None

    return classify


@dataclass(frozen=True)
class Customer:
    account: str
    name: str
    currency: str
    timezone: str
    #: The manager the customer is reached through ("" when reached directly).
    login_customer_id: str = ""


def customer_id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{10}", value):
        raise _fail("Invalid Google Ads customer ID.", "request_failed")
    return value


class GoogleAdsReports(GoogleReportSource):
    """Google Ads (`provider: google_ads`). Its reads need `developer_token` in
    `auth.credentials` too (the consent doesn't), and it takes `api_version` in `options`. An
    account is a customer id, or `customer:manager` for a customer reached through a manager; a
    report spec is `{"resource", "fields"}`."""

    provider = "google_ads"
    scope = SCOPE

    @property
    def api_version(self) -> str:
        return self.option("api_version", API_VERSION)

    def log_error(self, response: httpx.Response) -> None:
        log_google_ads_error(
            response, (self.credential("client_secret"), self.credential("developer_token"))
        )

    def summarize_error(self, response: httpx.Response) -> str:
        return google_ads_error_summary(
            response, (self.credential("client_secret"), self.credential("developer_token"))
        )

    def log_token_error(self, response: httpx.Response) -> None:
        self.log_error(response)

    def _client(self) -> ApiClient:
        return ApiClient(
            make_classify(self.log_error, self.summarize_error),
            unreachable=UNREACHABLE,
            invalid=INVALID,
            timeout=60,
            transport=self.transport,
            sleep=self.sleep,
        )

    async def headers(self, api: ApiClient, refresh_token: object) -> dict[str, str]:
        headers = await super().headers(api, refresh_token)
        if developer_token := self.credential("developer_token"):
            headers["developer-token"] = developer_token
        return headers

    def _base(self) -> str:
        if not re.fullmatch(r"v[0-9]+", self.api_version):
            raise _fail("Invalid Google Ads API version.", "request_failed")
        return f"https://googleads.googleapis.com/{self.api_version}"

    async def list_accounts(self, credentials: Mapping[str, Any]) -> list[Account]:
        """Every customer the user can read, one entry per route (directly, or through each
        manager, which is the `group`)."""
        return [
            Account(
                id=f"{c.account}:{c.login_customer_id}" if c.login_customer_id else c.account,
                name=c.name,
                group=c.login_customer_id,
                currency=c.currency,
                timezone=c.timezone,
                details={"customer_id": c.account, "login_customer_id": c.login_customer_id},
            )
            for c in await self.list_customers(refresh_token_of(credentials))
        ]

    async def read(
        self,
        credentials: Mapping[str, Any],
        account: str,
        spec: Mapping[str, Any],
        since: date,
        until: date,
    ) -> RawReport:
        customer, _, login = account.partition(":")
        return await self.read_report(
            refresh_token_of(credentials),
            customer,
            login,
            resource=spec["resource"],
            fields=list(spec["fields"]),
            since=since,
            until=until,
        )

    async def _search(
        self, api: ApiClient, headers: dict[str, str], account: str, query: str
    ) -> list[dict[str, Any]]:
        url = f"{self._base()}/customers/{customer_id(account)}/googleAds:search"

        async def fetch(token: str | None) -> dict[str, Any]:
            payload = {"query": query, **({"pageToken": token} if token else {})}
            return await api.json("POST", url, retry=True, headers=headers, json=payload)

        rows: list[dict[str, Any]] = []
        invalid = Problem("unavailable", "Google Ads returned invalid pagination.")
        async for body in token_pages(fetch, invalid=invalid):
            results = body.get("results", [])
            if not isinstance(results, list) or any(not isinstance(row, dict) for row in results):
                raise _fail("Google Ads returned invalid report rows.")
            if body.get("nextPageToken") and not results:
                raise invalid.error()
            rows.extend(results)
        return rows

    @staticmethod
    def _customer(detail: dict[str, Any], login: str) -> Customer:
        return Customer(
            customer_id(str(detail.get("id", ""))),
            str(detail.get("descriptiveName", "")),
            str(detail.get("currencyCode", "")),
            str(detail.get("timeZone", "")),
            login,
        )

    async def list_customers(self, refresh_token: str) -> list[Customer]:
        """Every customer the user can read: those reached directly, and the enabled clients of
        each manager account (one entry per route). Disabled customers are skipped."""
        found: dict[tuple[str, str], Customer] = {}
        async with self._client() as api:
            headers = await self.headers(api, refresh_token)
            body = await api.json(
                "GET", f"{self._base()}/customers:listAccessibleCustomers", headers=headers
            )
            roots = body.get("resourceNames", [])
            if not isinstance(roots, list):
                raise _fail("Google Ads returned an invalid customer list.")
            for resource in roots:
                if not isinstance(resource, str) or not re.fullmatch(
                    r"customers/[0-9]{10}", resource
                ):
                    raise _fail("Google Ads returned an invalid customer.")
                root = resource.split("/")[1]
                try:
                    own = await self._search(
                        api,
                        headers,
                        root,
                        "SELECT customer.id, customer.descriptive_name, customer.currency_code, "
                        "customer.time_zone, customer.manager FROM customer",
                    )
                    if len(own) != 1 or not isinstance(own[0].get("customer"), dict):
                        raise _fail("Google Ads returned invalid account details.")
                    detail = own[0]["customer"]
                    if not detail.get("manager"):
                        account = self._customer(detail, "")
                        found[(account.account, "")] = account
                        continue
                    clients = await self._search(
                        api,
                        {**headers, "login-customer-id": root},
                        root,
                        "SELECT customer_client.id, customer_client.descriptive_name, "
                        "customer_client.currency_code, "
                        "customer_client.time_zone, customer_client.manager FROM customer_client "
                        "WHERE customer_client.manager = FALSE "
                        "AND customer_client.status = 'ENABLED'",
                    )
                    for row in clients:
                        child = row.get("customerClient")
                        if not isinstance(child, dict) or child.get("manager"):
                            raise _fail("Google Ads returned invalid client details.")
                        account = self._customer(child, root)
                        found[(account.account, root)] = account
                except ReportError as exc:
                    if exc.reason != "customer_not_enabled":
                        raise
                    _logger.warning(f"Skipping Google Ads customer {root}: account is not enabled")
        return list(found.values())

    async def read_report(
        self,
        refresh_token: str,
        customer: str,
        login_customer: str,
        *,
        resource: str,
        fields: list[str],
        since: date,
        until: date,
    ) -> RawReport:
        """`fields` of `resource` (`campaign`, `ad_group`, `ad_group_ad`) by day over
        `since..until`, as Google returned the rows. `fields` must include `customer.id`,
        `segments.date`, `campaign.id` and the metrics; finer resources their own ids."""
        account = customer_id(customer)
        if login_customer:
            customer_id(login_customer)
        if not _FIELD.fullmatch(resource) or not all(_FIELD.fullmatch(f) for f in fields):
            raise _fail("Unknown Google Ads report definition.", "request_failed")
        records: list[dict[str, Any]] = []
        days: list[date | None] = []
        async with self._client() as api:
            headers = await self.headers(api, refresh_token)
            if login_customer:
                headers["login-customer-id"] = login_customer
            for start, end in windows(since, until, REPORT_WINDOW_DAYS):
                query = (
                    f"SELECT {', '.join(fields)} FROM {resource} "
                    f"WHERE segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"
                )
                for row in await self._search(api, headers, account, query):
                    _check_row(row, resource, account, start, end)
                    records.append(row)
                    days.append(parse_day(row["segments"]["date"]))
        _logger.info(f"Read {len(records)} {resource} rows for customer {account}")
        return RawReport(
            records, days, {"customer_id": account, "login_customer_id": login_customer}
        )


def _check_row(row: dict[str, Any], resource: str, account: str, start: date, end: date) -> None:
    segments, customer = row.get("segments"), row.get("customer")
    day = parse_day(segments.get("date", "")) if isinstance(segments, dict) else None
    if (
        day is None
        or not start <= day <= end
        or not isinstance(customer, dict)
        or str(customer.get("id")) != account
    ):
        raise _fail("Google Ads returned an invalid daily report row.")
    campaign = row.get("campaign")
    if not isinstance(row.get("metrics"), dict) or not (
        isinstance(campaign, dict) and campaign.get("id")
    ):
        raise _fail("Google Ads returned incomplete report rows.")
    ad_group = row.get("adGroup")
    if resource != "campaign" and not (isinstance(ad_group, dict) and ad_group.get("id")):
        raise _fail("Google Ads returned an invalid ad group.")
    ad_group_ad = row.get("adGroupAd")
    if resource == "ad_group_ad" and not (
        isinstance(ad_group_ad, dict)
        and isinstance(ad_group_ad.get("ad"), dict)
        and ad_group_ad["ad"].get("id")
    ):
        raise _fail("Google Ads returned an invalid ad.")
