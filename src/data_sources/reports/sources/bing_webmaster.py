"""Bing Webmaster Tools: a user's verified sites and four reports per site (search traffic by day,
crawl stats by day, top queries and top pages by week), through the Webmaster API's JSON endpoint.

Sign-in is Bing's own OAuth 2.0 (the client is registered in Bing Webmaster Tools under Settings →
API Access, not in Azure), read-only scope `webmaster.read`. An access token lasts an hour; the
refresh token, the docs say, until the user revokes access. A community report says a refresh can
hand back a new refresh token that Bing then refuses while the original keeps working, so the
consent's refresh token is kept apart (`consent_refresh_token`) and tried when the latest one is
refused.

The report methods take only the site: every call returns all the history Bing keeps (about six
months), with dates as WCF strings (`/Date(1316156400000-0700)/`). `read` keeps the rows dated
inside `since..until` and says what Bing had in the envelope (`available`). Records stay as Bing
returned them.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from data_sources.core.logging import logger
from data_sources.reports.base import Account, Grant, ReportSource
from data_sources.reports.errors import Problem, ReportAuthError, ReportError
from data_sources.reports.http import ApiClient, Retry, Verdict, linear
from data_sources.reports.models import RawReport
from data_sources.reports.oauth import consent_url

AUTH_URL = "https://www.bing.com/webmasters/oauth/authorize"
TOKEN_URL = "https://www.bing.com/webmasters/oauth/token"
API_URL = "https://ssl.bing.com/webmaster/api.svc/json/"
SCOPE = "webmaster.read"

#: Report key -> the API method that serves it.
REPORTS = {
    "traffic": "GetRankAndTrafficStats",
    "crawl": "GetCrawlStats",
    "queries": "GetQueryStats",
    "pages": "GetPageStats",
}
#: Numeric fields of each report's rows (checked, never converted).
METRICS = {
    "traffic": ("Clicks", "Impressions"),
    "crawl": (
        "CrawledPages",
        "CrawlErrors",
        "InIndex",
        "InLinks",
        "Code2xx",
        "Code301",
        "Code302",
        "Code4xx",
        "Code5xx",
        "AllOtherCodes",
        "BlockedByRobotsTxt",
        "ContainsMalware",
    ),
    "queries": ("Clicks", "Impressions", "AvgClickPosition", "AvgImpressionPosition"),
    "pages": ("Clicks", "Impressions", "AvgClickPosition", "AvgImpressionPosition"),
}
#: Reports whose rows name a query (or, for pages, the page URL) in `Query`.
NAMED = {"queries", "pages"}

#: `ApiErrorCode` values that mean the grant is no good (InvalidApiKey, UserBlocked,
#: NotAuthorized and 10) and those worth retrying (InternalError, ThrottleUser, ThrottleHost).
AUTH_CODES = {3, 6, 10, 14}
RETRY_CODES = {1, 4, 5}

UNREACHABLE = Problem("unavailable", "Bing Webmaster could not be reached.")
BUSY = Problem("unavailable", "Bing Webmaster is temporarily unavailable or rate limited.")
INVALID = Problem("unavailable", "Bing Webmaster returned an invalid response.")
AUTH_FAILED = Problem(
    "auth_failed", "Bing Webmaster refused the token or a permission; authorize the source again."
)

_WCF_DATE = re.compile(r"^/Date\((-?\d+)([+-])?(\d{2})?(\d{2})?\)/$")

_logger = logger.getChild("bing_webmaster.reports")


def _now() -> datetime:
    return datetime.now(UTC)


def parse_wcf_date(value: object) -> date | None:
    """The day of a WCF JSON date: `/Date(<ms since epoch>[±hhmm])/` is a UTC instant, shown in
    the offset's local time when there is one. Anything else is None."""
    if not isinstance(value, str):
        return None
    match = _WCF_DATE.match(value)
    if not match:
        return None
    millis, sign, hours, minutes = match.groups()
    if sign is not None and (hours is None or minutes is None):
        return None
    try:
        instant = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=int(millis))
    except OverflowError:
        return None
    if sign is not None:
        offset = timedelta(hours=int(hours), minutes=int(minutes))
        instant = instant + offset if sign == "+" else instant - offset
    return instant.date()


def _fault(response: httpx.Response) -> tuple[int | None, str]:
    """`(ErrorCode, Message)` of an `ApiFault` body, `(None, "")` when it isn't one."""
    try:
        body = response.json()
    except ValueError:
        return None, ""
    if isinstance(body, dict) and isinstance(body.get("d"), dict):
        body = body["d"]
    if not isinstance(body, dict):
        return None, ""
    code = body.get("ErrorCode")
    if not isinstance(code, int) or isinstance(code, bool):
        return None, ""
    return code, " ".join(str(body.get("Message") or "").split())


def classify(response: httpx.Response) -> Verdict:
    """Bing answers a fault (`ErrorCode`, `Message`) with an error status; both are read."""
    code, message = _fault(response)
    if response.status_code == 429 or response.status_code >= 500 or code in RETRY_CODES:
        return Retry(BUSY)
    if response.status_code in {401, 403} or code in AUTH_CODES:
        return AUTH_FAILED
    if response.is_error or code is not None:
        detail = f"{code}: {message}" if code is not None else f"HTTP {response.status_code}"
        return Problem("request_failed", "Bing Webmaster refused the request.", detail[:300])
    return None


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


class BingWebmasterReports(ReportSource):
    """Bing Webmaster Tools (`provider: bing_webmaster`): `client_id`, `client_secret` and
    `redirect_uri` in `auth.credentials`. The grant's credentials are `{"access_token",
    "refresh_token", "consent_refresh_token", "expires_at"}` (ISO, UTC); an account is a verified
    site URL; a report spec is `{"report": "traffic" | "crawl" | "queries" | "pages"}`."""

    provider = "bing_webmaster"
    app_credentials = ("client_id", "client_secret")

    def _client(self, token: str) -> ApiClient:
        return ApiClient(
            classify,
            unreachable=UNREACHABLE,
            invalid=INVALID,
            base_url=API_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
            backoff=linear(),
            transport=self.transport,
            sleep=self.sleep,
        )

    def authorize_url(self, state: str) -> str:
        return consent_url(
            AUTH_URL,
            response_type="code",
            client_id=self.credential("client_id"),
            redirect_uri=self.credential("redirect_uri"),
            scope=SCOPE,
            state=state,
        )

    async def _token_request(self, params: dict[str, str]) -> dict[str, Any]:
        # Not through `classify`: the token endpoint answers OAuth errors
        # (`{"error": "invalid_grant", ...}`), not API faults.
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
                "Bing refused the authorization code or the refresh token.",
                " ".join(str(reason).split())[:300],
            )
        if not isinstance(body, dict):
            raise INVALID.error()
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise INVALID.error()
        return body

    @staticmethod
    def _expires_at(body: Mapping[str, Any]) -> str:
        # Without a lifetime the token counts as expired: the next read refreshes it first.
        expires = body.get("expires_in")
        seconds = int(expires) if isinstance(expires, int | float) and _is_number(expires) else 0
        expires_at: datetime = _now() + timedelta(seconds=max(seconds, 0))
        return expires_at.isoformat()

    async def exchange_code(self, code: str) -> Grant:
        """The tokens the consent's `code` buys. Bing names nobody in its answer, so the grant has
        no identity."""
        body = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.credential("redirect_uri"),
            }
        )
        # Names only, never values: what Bing's answer holds is still unverified.
        _logger.info(f"Bing token exchange answered with keys {sorted(body)}")
        refresh = body.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            raise ReportError("no_refresh_token", "Bing returned no refresh token.")
        return Grant(
            {
                "access_token": str(body["access_token"]),
                "refresh_token": refresh,
                "consent_refresh_token": refresh,
                "expires_at": self._expires_at(body),
            }
        )

    async def refresh_grant(self, credentials: Mapping[str, Any]) -> Grant:
        """A fresh access token for a grant whose access token has expired. The latest refresh
        token is tried first, then the consent's when Bing refuses it (see the module's
        docstring); a refresh token Bing hands back becomes the latest. Fails with `auth_failed`
        once both are refused: the user has to consent again."""
        latest = credentials.get("refresh_token")
        consent = credentials.get("consent_refresh_token")
        tries = [
            (label, token)
            for label, token in (("latest", latest), ("consent", consent))
            if isinstance(token, str) and token
        ]
        if len(tries) == 2 and tries[0][1] == tries[1][1]:
            tries = tries[:1]
        if not tries:
            raise AUTH_FAILED.error()
        refused: ReportAuthError | None = None
        for label, token in tries:
            try:
                body = await self._token_request(
                    {"grant_type": "refresh_token", "refresh_token": token}
                )
            except ReportAuthError as exc:
                refused = exc
                continue
            returned = body.get("refresh_token")
            rotated = isinstance(returned, str) and bool(returned) and returned != token
            _logger.info(f"Bing token refresh: rotated={'yes' if rotated else 'no'} used={label}")
            keep = returned if isinstance(returned, str) and returned else token
            return Grant(
                {
                    "access_token": str(body["access_token"]),
                    "refresh_token": keep,
                    "consent_refresh_token": consent
                    if isinstance(consent, str) and consent
                    else token,
                    "expires_at": self._expires_at(body),
                }
            )
        assert refused is not None
        raise ReportAuthError(
            "auth_failed",
            "Bing refused the refresh token; authorize the source again.",
            refused.diagnostics,
        )

    @staticmethod
    def _token(credentials: Mapping[str, Any]) -> str:
        token = credentials.get("access_token")
        if not isinstance(token, str) or not token:
            raise AUTH_FAILED.error()
        return token

    @staticmethod
    def _rows(body: Mapping[str, Any]) -> list[Any]:
        rows = body.get("d")
        if not isinstance(rows, list):
            raise INVALID.error()
        return rows

    async def list_accounts(self, credentials: Mapping[str, Any]) -> list[Account]:
        """The user's verified sites."""
        async with self._client(self._token(credentials)) as api:
            sites = self._rows(await api.json("GET", "GetUserSites"))
        if any(
            not isinstance(s, dict) or not isinstance(s.get("Url"), str) or not s["Url"]
            for s in sites
        ):
            raise INVALID.error()
        return [
            Account(id=s["Url"], name=s["Url"], details={"verified": True})
            for s in sites
            if s.get("IsVerified") is True
        ]

    async def read(
        self,
        credentials: Mapping[str, Any],
        account: str,
        spec: Mapping[str, Any],
        since: date,
        until: date,
    ) -> RawReport:
        """The rows of `spec["report"]` dated in `since..until`: Bing has no date parameters and
        returns every row it keeps, so the window is applied here."""
        report = spec.get("report")
        if not isinstance(report, str) or report not in REPORTS:
            raise ReportError("request_failed", "Unsupported Bing Webmaster report.")
        if since > until:
            raise ReportError("request_failed", "The report's start is after its end.")
        if not account:
            raise ReportError("request_failed", "No site was given.")
        method = REPORTS[report]
        async with self._client(self._token(credentials)) as api:
            body = await api.json("GET", method, params={"siteUrl": account}, retry=True)
        rows = self._rows(body)
        dated: list[tuple[dict[str, Any], date]] = []
        for row in rows:
            day = parse_wcf_date(row.get("Date")) if isinstance(row, dict) else None
            if day is None or not isinstance(row, dict):
                raise INVALID.error()
            if any(
                row.get(name) is not None and not _is_number(row[name]) for name in METRICS[report]
            ):
                raise INVALID.error()
            if report in NAMED and (not isinstance(row.get("Query"), str) or not row["Query"]):
                raise INVALID.error()
            dated.append((row, day))
        kept = [(row, day) for row, day in dated if since <= day <= until]
        all_days = [day for _, day in dated]
        envelope = {
            "siteUrl": account,
            "report": report,
            "method": method,
            "available": {
                "rows": len(dated),
                "first": min(all_days).isoformat() if all_days else None,
                "last": max(all_days).isoformat() if all_days else None,
            },
        }
        _logger.info(f"Read {len(kept)} of {len(dated)} {report} rows for {account}")
        return RawReport([row for row, _ in kept], [day for _, day in kept], envelope)
