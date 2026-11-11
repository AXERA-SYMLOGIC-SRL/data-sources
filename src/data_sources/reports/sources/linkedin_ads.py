"""LinkedIn Ads, read-only, on the Marketing API's versioned REST endpoints
(`api.linkedin.com/rest`): ad accounts, four daily `adAnalytics` reports and three snapshots of an
account's entities.

Sign-in is LinkedIn's 3-legged OAuth with `r_ads` and `r_ads_reporting`. An access token lasts 60
days; a refresh token (365 days, not extended by use) is only issued to some partner programs, so a
grant may have none and then simply stops working when its access token expires.

Rest.li query values (`List(...)`, `(start:(year:...))`) are built by hand, because their
parentheses and commas must reach LinkedIn unescaped while the colons in URNs are escaped.
Everything put in a query is checked first (account ids are digits, pivots and metrics come from
fixed patterns). Records stay as LinkedIn returned them, money included (decimal strings).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from data_sources.core.logging import logger
from data_sources.reports.base import Account, Grant, ReportSource
from data_sources.reports.errors import Problem, ReportAuthError, ReportError
from data_sources.reports.http import ApiClient, Retry, Verdict
from data_sources.reports.models import RawReport
from data_sources.reports.oauth import consent_url
from data_sources.reports.windows import windows

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
API_BASE = "https://api.linkedin.com/rest/"
SCOPES = ("r_ads", "r_ads_reporting")
DEFAULT_VERSION = "202610"

PIVOTS = ("ACCOUNT", "CAMPAIGN_GROUP", "CAMPAIGN", "CREATIVE")
#: The entity a pivot's `pivotValues` URN names, and what a URN's id is checked against.
URN_KINDS = {
    "ACCOUNT": "sponsoredAccount",
    "CAMPAIGN_GROUP": "sponsoredCampaignGroup",
    "CAMPAIGN": "sponsoredCampaign",
    "CREATIVE": "sponsoredCreative",
}
REPORT_WINDOW_DAYS = 30
#: `adAnalytics` returns at most this many elements and does not page: a response this full may
#: have been cut, so the window is split.
ANALYTICS_LIMIT = 15000
#: At most 20 fields per request, two of them `dateRange` and `pivotValues`.
FIELDS_PER_REQUEST = 18

#: Entity snapshots: the finder, its fixed query, the page size and the statuses LinkedIn must be
#: asked for (the search criteria are mandatory, and without a status filter archived and removed
#: entities are hidden).
GROUP_STATUSES = (
    "ACTIVE",
    "ARCHIVED",
    "CANCELED",
    "DRAFT",
    "PAUSED",
    "PENDING_DELETION",
    "REMOVED",
)
CAMPAIGN_STATUSES = (
    "ACTIVE",
    "PAUSED",
    "ARCHIVED",
    "COMPLETED",
    "CANCELED",
    "DRAFT",
    "PENDING_DELETION",
    "REMOVED",
)
ENTITIES: dict[str, tuple[str, str, int, dict[str, str]]] = {
    "campaign_groups": (
        "adCampaignGroups",
        f"q=search&search=(status:(values:List({','.join(GROUP_STATUSES)})))",
        1000,
        {},
    ),
    "campaigns": (
        "adCampaigns",
        f"q=search&search=(status:(values:List({','.join(CAMPAIGN_STATUSES)})))",
        1000,
        {},
    ),
    "creatives": ("creatives", "q=criteria", 100, {"X-RestLi-Method": "FINDER"}),
}
MAX_PAGES = 200
ACCOUNT_PAGES = 100
ACCOUNT_PAGE_SIZE = 100
MAX_RETRY_AFTER = 30.0

UNREACHABLE = Problem("unavailable", "LinkedIn could not be reached.")
BUSY = Problem("unavailable", "LinkedIn is temporarily unavailable or rate limited; retry later.")
INVALID = Problem("unavailable", "LinkedIn returned an invalid response.")
AUTH_FAILED = Problem(
    "auth_failed", "LinkedIn refused the token or a permission; authorize the source again."
)
VERSION_UNSUPPORTED = Problem(
    "version_unsupported", "LinkedIn no longer supports the API version in use; raise it."
)

_METRIC = re.compile(r"^[A-Za-z]+$")
_logger = logger.getChild("linkedin_ads.reports")


def _now() -> datetime:
    return datetime.now(UTC)


def _today() -> date:
    return _now().date()


def _detail(response: httpx.Response) -> str:
    """`serviceErrorCode` and `message` of an error body (never the request); "" when it has
    neither."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    parts = []
    code = body.get("serviceErrorCode")
    if isinstance(code, int) and not isinstance(code, bool):
        parts.append(str(code))
    message = " ".join(str(body.get("message") or "").split())
    if message:
        parts.append(message)
    return ": ".join(parts)[:300]


def _retry_after(response: httpx.Response) -> float | None:
    try:
        wait = float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None
    return min(max(wait, 0.0), MAX_RETRY_AFTER)


def classify(response: httpx.Response) -> Verdict:
    """429 and 5xx are retried; 401 and 403 are the grant's fault; 426 is a retired API version;
    anything else LinkedIn refuses is `request_failed`, with its own error code and message."""
    status = response.status_code
    if status == 429 or status >= 500:
        return Retry(BUSY, _retry_after(response))
    if status in {401, 403}:
        return Problem("auth_failed", AUTH_FAILED.message, _detail(response))
    if status == 426:
        return Problem("version_unsupported", VERSION_UNSUPPORTED.message, _detail(response))
    if response.is_error:
        detail = _detail(response) or f"HTTP {status}"
        return Problem("request_failed", "LinkedIn refused the request.", detail)
    return None


def _account_id(account: object) -> str:
    if not isinstance(account, str) or not account.isascii() or not account.isdigit():
        raise ReportError("request_failed", "The LinkedIn ad account id is not valid.")
    return account


def _date_range(start: date, end: date) -> str:
    def part(day: date) -> str:
        return f"(year:{day.year},month:{day.month},day:{day.day})"

    return f"(start:{part(start)},end:{part(end)})"


def _row_day(value: object) -> date | None:
    """The day of a `{"year", "month", "day"}` object."""
    if not isinstance(value, dict):
        return None
    year, month, day = value.get("year"), value.get("month"), value.get("day")
    if any(not isinstance(v, int) or isinstance(v, bool) for v in (year, month, day)):
        return None
    try:
        return date(year, month, day)  # type: ignore[arg-type]
    except ValueError:
        return None


def _seconds(value: object) -> int | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
        return int(value)
    return None


class LinkedInAdsReports(ReportSource):
    """LinkedIn Ads (`provider: linkedin_ads`): `client_id`, `client_secret` and `redirect_uri` in
    `auth.credentials`, `api_version` (`YYYYMM`) in the options. The grant's credentials are
    `{"access_token", "expires_at", "refresh_token"?, "refresh_expires_at"?, "scope"}` (ISO, UTC);
    an account is an ad account id (digits). A report spec is `{"pivot": "ACCOUNT" |
    "CAMPAIGN_GROUP" | "CAMPAIGN" | "CREATIVE", "metrics": [...]}` (a daily `adAnalytics` report)
    or `{"entity": "campaign_groups" | "campaigns" | "creatives"}` (every one of the account, as
    it is now, labelled with today's UTC day)."""

    provider = "linkedin_ads"
    app_credentials = ("client_id", "client_secret")

    def _client(self, token: str) -> ApiClient:
        return ApiClient(
            classify,
            unreachable=UNREACHABLE,
            invalid=INVALID,
            base_url=API_BASE,
            headers={
                "Authorization": f"Bearer {token}",
                "Linkedin-Version": self.option("api_version", DEFAULT_VERSION),
                "X-Restli-Protocol-Version": "2.0.0",
            },
            timeout=60,
            transport=self.transport,
            sleep=self.sleep,
        )

    def authorize_url(self, state: str) -> str:
        return consent_url(
            AUTH_URL,
            response_type="code",
            client_id=self.credential("client_id"),
            redirect_uri=self.credential("redirect_uri"),
            scope=" ".join(SCOPES),
            state=state,
        )

    async def _token_request(self, params: dict[str, str]) -> dict[str, Any]:
        # Not through `classify` and not retried: the token endpoint answers OAuth errors, and an
        # authorization code works once.
        try:
            async with httpx.AsyncClient(timeout=15, transport=self.transport) as http:
                response = await http.post(
                    TOKEN_URL,
                    data={
                        "client_id": self.credential("client_id"),
                        "client_secret": self.credential("client_secret"),
                        **params,
                    },
                )
        except httpx.HTTPError:
            raise UNREACHABLE.error() from None
        if response.status_code == 429 or response.status_code >= 500:
            raise BUSY.error()
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.is_error or (isinstance(body, dict) and body.get("error")):
            found = body if isinstance(body, dict) else {}
            reason = found.get("error_description") or found.get("error") or ""
            raise ReportAuthError(
                "auth_failed",
                "LinkedIn refused the authorization code or the refresh token.",
                " ".join(str(reason).split())[:300],
            )
        if not isinstance(body, dict):
            raise INVALID.error()
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise INVALID.error()
        return body

    @staticmethod
    def _grant(body: Mapping[str, Any], previous: Mapping[str, Any] | None = None) -> Grant:
        """The grant a token answer makes. A lifetime LinkedIn doesn't give counts as already
        over, so the next read renews the token (or fails with `auth_failed`) rather than
        sending a stale one."""
        now = _now()
        access_for = _seconds(body.get("expires_in")) or 0
        credentials: dict[str, Any] = {
            "access_token": str(body["access_token"]),
            "expires_at": (now + timedelta(seconds=access_for)).isoformat(),
        }
        old = previous or {}
        scope = body.get("scope") or old.get("scope")
        if isinstance(scope, str) and scope:
            credentials["scope"] = scope
        refresh = body.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            refresh = old.get("refresh_token")
        expires_in = access_for
        if isinstance(refresh, str) and refresh:
            credentials["refresh_token"] = refresh
            refresh_for = _seconds(body.get("refresh_token_expires_in"))
            if refresh_for is not None:
                credentials["refresh_expires_at"] = (
                    now + timedelta(seconds=refresh_for)
                ).isoformat()
                expires_in = refresh_for
            elif isinstance(old.get("refresh_expires_at"), str):
                credentials["refresh_expires_at"] = old["refresh_expires_at"]
        return Grant(credentials, expires_in=expires_in)

    async def exchange_code(self, code: str) -> Grant:
        """The tokens the consent's `code` buys. Without `r_ads_reporting` (the consent screen
        lets a user decline it) nothing can be read, so the consent is refused."""
        body = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.credential("redirect_uri"),
            }
        )
        scope = body.get("scope")
        if scope is not None and (
            not isinstance(scope, str) or "r_ads_reporting" not in re.split(r"[,\s]+", scope)
        ):
            raise ReportError("access_denied", "The consent didn't grant the access asked for.")
        return self._grant(body)

    async def refresh_grant(self, credentials: Mapping[str, Any]) -> Grant:
        """A fresh access token. Only grants with a refresh token can be renewed; any other fails
        with `auth_failed`, and so does a refresh token LinkedIn refuses."""
        refresh = credentials.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            raise AUTH_FAILED.error()
        body = await self._token_request({"grant_type": "refresh_token", "refresh_token": refresh})
        return self._grant(body, credentials)

    @staticmethod
    def _token(credentials: Mapping[str, Any]) -> str:
        token = credentials.get("access_token")
        if not isinstance(token, str) or not token:
            raise AUTH_FAILED.error()
        return token

    @staticmethod
    def _elements(body: Mapping[str, Any]) -> list[Any]:
        elements = body.get("elements")
        if not isinstance(elements, list):
            raise INVALID.error()
        return elements

    @staticmethod
    def _next_page(body: Mapping[str, Any]) -> str | None:
        metadata = body.get("metadata")
        token = metadata.get("nextPageToken") if isinstance(metadata, dict) else None
        if token is None or token == "":
            return None
        if not isinstance(token, str):
            raise INVALID.error()
        return token

    async def list_accounts(self, credentials: Mapping[str, Any]) -> list[Account]:
        """The ad accounts the user can read."""
        found: list[Account] = []
        token: str | None = None
        async with self._client(self._token(credentials)) as api:
            for _ in range(ACCOUNT_PAGES):
                query = f"adAccounts?q=search&pageSize={ACCOUNT_PAGE_SIZE}"
                if token:
                    query += f"&pageToken={quote(token, safe='')}"
                body = await api.json("GET", query)
                for item in self._elements(body):
                    found.append(self._account(item))
                token = self._next_page(body)
                if token is None:
                    return found
        raise ReportError("request_failed", "LinkedIn has too many ad accounts to list.")

    @staticmethod
    def _account(item: object) -> Account:
        if not isinstance(item, dict):
            raise INVALID.error()
        identifier = item.get("id")
        if isinstance(identifier, bool) or not isinstance(identifier, int | str):
            raise INVALID.error()
        account = str(identifier)
        if not account.isascii() or not account.isdigit():
            raise INVALID.error()
        name = " ".join(str(item.get("name") or "").split()) or account
        details = {key: item[key] for key in ("status", "type") if isinstance(item.get(key), str)}
        return Account(
            id=account,
            name=name[:300],
            currency=str(item.get("currency") or ""),
            details=details,
        )

    async def read(
        self,
        credentials: Mapping[str, Any],
        account: str,
        spec: Mapping[str, Any],
        since: date,
        until: date,
    ) -> RawReport:
        """The report `spec` describes: an `adAnalytics` report by day over `since..until`, or the
        account's entities of one kind as they are now (the window is not used then)."""
        account = _account_id(account)
        if "entity" in spec:
            return await self._read_entities(credentials, account, spec.get("entity"))
        pivot, metrics = spec.get("pivot"), spec.get("metrics")
        if pivot not in PIVOTS:
            raise ReportError("request_failed", "Unsupported LinkedIn Ads report pivot.")
        if (
            not isinstance(metrics, list | tuple)
            or not metrics
            or len(set(metrics)) != len(metrics)
            or any(not isinstance(m, str) or not _METRIC.match(m) for m in metrics)
            or {"dateRange", "pivotValues"} & set(metrics)
        ):
            raise ReportError("request_failed", "Invalid LinkedIn Ads report metrics.")
        if since > until:
            raise ReportError("request_failed", "The report's start is after its end.")
        records: list[dict[str, Any]] = []
        days: list[date | None] = []
        requests = 0
        async with self._client(self._token(credentials)) as api:
            for start, end in windows(since, until, REPORT_WINDOW_DAYS):
                rows, used = await self._analytics(api, account, pivot, list(metrics), start, end)
                requests += used
                for row, day in rows:
                    records.append(row)
                    days.append(day)
        _logger.info(
            f"Read {len(records)} {pivot} rows for ad account {account} in {requests} requests"
        )
        return RawReport(records, days, {"account": account, "pivot": pivot, "requests": requests})

    async def _analytics(
        self,
        api: ApiClient,
        account: str,
        pivot: str,
        metrics: list[str],
        start: date,
        end: date,
    ) -> tuple[list[tuple[dict[str, Any], date]], int]:
        """The rows of one window, merged over the field chunks, and how many requests it took.
        A window LinkedIn filled to its limit is halved and each half read again."""
        merged: dict[tuple[str, date], dict[str, Any]] = {}
        requests = 0
        for offset in range(0, len(metrics), FIELDS_PER_REQUEST):
            chunk = metrics[offset : offset + FIELDS_PER_REQUEST]
            fields = ",".join([*chunk, "dateRange", "pivotValues"])
            body = await api.json(
                "GET",
                f"adAnalytics?q=analytics&pivot={pivot}&timeGranularity=DAILY"
                f"&dateRange={_date_range(start, end)}"
                f"&accounts=List(urn%3Ali%3AsponsoredAccount%3A{account})&fields={fields}",
            )
            requests += 1
            elements = self._elements(body)
            if len(elements) >= ANALYTICS_LIMIT:
                if start == end:
                    raise ReportError(
                        "request_failed", "A single day of this report is too large for LinkedIn."
                    )
                middle = start + timedelta(days=(end - start).days // 2)
                left, used_left = await self._analytics(api, account, pivot, metrics, start, middle)
                right, used_right = await self._analytics(
                    api, account, pivot, metrics, middle + timedelta(days=1), end
                )
                return left + right, requests + used_left + used_right
            seen: set[tuple[str, date]] = set()
            for element in elements:
                key = self._key(element, pivot, start, end)
                if key in seen:
                    raise INVALID.error()
                seen.add(key)
                if offset == 0:
                    merged[key] = dict(element)
                elif key not in merged:
                    raise INVALID.error()
                else:
                    merged[key].update(element)
            if offset and seen != merged.keys():
                raise INVALID.error()
        return [(row, key[1]) for key, row in merged.items()], requests

    @staticmethod
    def _key(element: object, pivot: str, start: date, end: date) -> tuple[str, date]:
        """`(entity URN, day)` of an `adAnalytics` element, which must be one entity on one day
        inside the window."""
        if not isinstance(element, dict):
            raise INVALID.error()
        values = element.get("pivotValues")
        if (
            not isinstance(values, list)
            or len(values) != 1
            or not isinstance(values[0], str)
            or not values[0].startswith(f"urn:li:{URN_KINDS[pivot]}:")
        ):
            raise INVALID.error()
        found = element.get("dateRange")
        first = _row_day(found.get("start")) if isinstance(found, dict) else None
        last = _row_day(found.get("end")) if isinstance(found, dict) else None
        if first is None or first != last or not start <= first <= end:
            raise INVALID.error()
        return values[0], first

    async def _read_entities(
        self, credentials: Mapping[str, Any], account: str, entity: object
    ) -> RawReport:
        if not isinstance(entity, str) or entity not in ENTITIES:
            raise ReportError("request_failed", "Unsupported LinkedIn Ads entity.")
        path, query, page_size, headers = ENTITIES[entity]
        records: list[dict[str, Any]] = []
        token: str | None = None
        pages = 0
        async with self._client(self._token(credentials)) as api:
            while True:
                pages += 1
                if pages > MAX_PAGES:
                    raise ReportError(
                        "request_failed", f"LinkedIn has too many {entity} to read in one import."
                    )
                url = f"adAccounts/{account}/{path}?{query}&pageSize={page_size}"
                if token:
                    url += f"&pageToken={quote(token, safe='')}"
                body = await api.json("GET", url, headers=headers)
                for item in self._elements(body):
                    identifier = item.get("id") if isinstance(item, dict) else None
                    if (
                        isinstance(identifier, bool)
                        or not isinstance(identifier, int | str)
                        or identifier == ""
                    ):
                        raise INVALID.error()
                    records.append(item)
                token = self._next_page(body)
                if token is None:
                    break
        _logger.info(f"Read {len(records)} {entity} for ad account {account} in {pages} pages")
        return RawReport(
            records,
            [_today()] * len(records),
            {"account": account, "entity": entity, "pages": pages},
        )
