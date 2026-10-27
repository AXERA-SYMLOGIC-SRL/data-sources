"""Meta Ads through the Graph API: Facebook Login, the user's ad accounts, insights and custom
conversions.

Meta has no refresh tokens: the consent's code buys a short-lived token, which is extended to a
long-lived one (about 60 days); after that the user consents again. Every call carries
`appsecret_proof`, and the token goes in a header, never in the URL.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from data_sources.core.logging import logger
from data_sources.reports.base import Account, Grant, ReportSource
from data_sources.reports.errors import Problem, ReportAuthError, ReportError
from data_sources.reports.http import ApiClient, Retry, Verdict, exponential
from data_sources.reports.models import RawReport, parse_day
from data_sources.reports.oauth import consent_url

API_VERSION = "v26.0"
SCOPES = ("ads_read",)
#: Insights rows asked for per request.
PAGE_SIZE = 500
MAX_PAGES = 10_000
_ACCOUNT = re.compile(r"^act_\d+$")

#: Graph error codes that mean "slow down": app-level (4, 17, 32, 341, 613) and the ad-account
#: ones (80000-80014 are the Business Use Case limits).
THROTTLE_CODES = {4, 17, 32, 341, 613, *range(80000, 80015)}
#: An invalid or expired token (102, 190), or a missing permission (10, 200-299).
AUTH_CODES = {102, 190, 10, *range(200, 300)}
#: A throttle that Meta says lasts longer than this fails the request instead of waiting.
MAX_THROTTLE_WAIT = 120.0

UNAVAILABLE = Problem("unavailable", "Meta couldn't be reached or refused the request.")
BUSY = Problem("unavailable", "Meta kept throttling the request; try again later.")
INVALID = Problem("unavailable", "Meta returned an invalid response.")
AUTH_FAILED = Problem(
    "auth_failed", "Meta refused the token: it is invalid, expired or missing a permission."
)

_logger = logger.getChild("meta_ads.reports")


def appsecret_proof(access_token: str, app_secret: str) -> str:
    """Proves a call comes from the app's server, not from wherever the token leaked to."""
    return hmac.new(app_secret.encode(), access_token.encode(), hashlib.sha256).hexdigest()


def _graph_error(response: httpx.Response) -> int | None:
    try:
        code = response.json()["error"].get("code")
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return code if isinstance(code, int) else None


def _regain_seconds(response: httpx.Response) -> float | None:
    """How long Meta says the ad account is blocked (`x-business-use-case-usage`), if it says."""
    try:
        usage = json.loads(response.headers["x-business-use-case-usage"])
        minutes = max(
            entry.get("estimated_time_to_regain_access", 0)
            for entries in usage.values()
            for entry in entries
        )
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
    return float(minutes) * 60 if minutes else None


def classify(response: httpx.Response) -> Verdict:
    if response.status_code < 400:
        return None
    code = _graph_error(response)
    if code in THROTTLE_CODES or response.status_code >= 500:
        wait = _regain_seconds(response)
        if wait is not None and wait > MAX_THROTTLE_WAIT:
            return Problem(
                "unavailable", f"Meta throttled this ad account for about {wait / 60:.0f} minutes."
            )
        return Retry(BUSY, wait)
    if code in AUTH_CODES or response.status_code in (401, 403):
        return AUTH_FAILED
    return UNAVAILABLE


@dataclass(frozen=True)
class AdAccount:
    account: str  # `act_{id}`
    name: str
    currency: str
    status: int  # Meta's `account_status`; 1 is active
    timezone: str


@dataclass(frozen=True)
class CustomConversion:
    """A conversion defined on the ad account (Events Manager). Its results show up in an
    insights record's `actions` / `action_values` as `action_type`
    `offsite_conversion.custom.<id>`, and only on days it happened."""

    id: str
    name: str
    event_type: str
    description: str
    default_value: float | None

    @property
    def action_type(self) -> str:
        return f"offsite_conversion.custom.{self.id}"


def parse_custom_conversions(items: list[dict[str, Any]]) -> list[CustomConversion]:
    return [
        CustomConversion(
            id=str(item["id"]),
            name=item.get("name", ""),
            event_type=item.get("custom_event_type", ""),
            description=item.get("description", ""),
            default_value=item.get("default_conversion_value"),
        )
        for item in items
    ]


def check_ad_account(value: object) -> str:
    if not isinstance(value, str) or not _ACCOUNT.match(value):
        raise ReportError("unavailable", "The source is not bound to a Meta ad account.")
    return value


class MetaAdsReports(ReportSource):
    """Meta Ads (`provider: meta_ads`): `app_id`, `app_secret` and `redirect_uri` in
    `auth.credentials`, `api_version` in `options`. The grant's credentials are
    `{"access_token"}`; an account is an ad account (`act_{id}`); a report spec is
    `{"fields", "level", "breakdowns"?, "time_increment"?, "day_column"?}`."""

    provider = "meta_ads"
    app_credentials = ("app_id", "app_secret")

    @property
    def graph_url(self) -> str:
        return f"https://graph.facebook.com/{self.option('api_version', API_VERSION)}"

    @property
    def dialog_url(self) -> str:
        return f"https://www.facebook.com/{self.option('api_version', API_VERSION)}/dialog/oauth"

    def _client(self, timeout: float = 60) -> ApiClient:
        return ApiClient(
            classify,
            unreachable=UNAVAILABLE,
            invalid=INVALID,
            timeout=timeout,
            attempts=5,
            backoff=exponential(2.0),
            transport=self.transport,
            sleep=self.sleep,
        )

    async def _get(
        self, api: ApiClient, token: str, path: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        query = dict(params)
        if app_secret := self.credential("app_secret"):
            query["appsecret_proof"] = appsecret_proof(token, app_secret)
        return await api.json(
            "GET",
            f"{self.graph_url}/{path}",
            params=query,
            headers={"Authorization": f"Bearer {token}"},
        )

    async def _pages(
        self, api: ApiClient, token: str, path: str, params: dict[str, Any]
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Each page of `data` from a cursor-paged edge."""
        query = dict(params)
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            page = await self._get(api, token, path, query)
            data = page.get("data", [])
            if not isinstance(data, list):
                raise INVALID.error()
            yield data
            paging = page.get("paging", {})
            cursors = paging.get("cursors", {}) if isinstance(paging, dict) else {}
            after = cursors.get("after") if isinstance(cursors, dict) else None
            if not data or not after or "next" not in paging:
                return
            if not isinstance(after, str) or after in seen:
                raise INVALID.error()  # a cursor that doesn't move would page forever
            seen.add(after)
            query["after"] = after
        raise INVALID.error()

    @staticmethod
    def _token(access_token: object) -> str:
        if not isinstance(access_token, str) or not access_token:
            raise AUTH_FAILED.error()
        return access_token

    def authorize_url(self, state: str) -> str:
        return consent_url(
            self.dialog_url,
            client_id=self.credential("app_id"),
            redirect_uri=self.credential("redirect_uri"),
            state=state,
            scope=",".join(SCOPES),
            response_type="code",
        )

    async def _token_request(
        self, http: httpx.AsyncClient, params: dict[str, str]
    ) -> tuple[str, int | None]:
        # POSTed, though Meta's docs show GET: the app secret, the code and the tokens would
        # otherwise sit in the URL.
        response = await http.post(f"{self.graph_url}/oauth/access_token", data=params)
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {}
        if (
            response.status_code >= 400
            or not isinstance(body, dict)
            or not body.get("access_token")
        ):
            raise ReportAuthError("auth_failed", "Meta refused the token request.")
        try:
            expires = int(body.get("expires_in") or 0) or None
        except (TypeError, ValueError):
            expires = None  # Meta didn't say in a usable way: treat the token as not expiring
        return str(body["access_token"]), expires

    async def exchange_code(self, code: str) -> Grant:
        """The long-lived token the consent's `code` buys (`{"access_token"}`), how long it
        lasts, and who gave it (best effort)."""
        app = {
            "client_id": self.credential("app_id"),
            "client_secret": self.credential("app_secret"),
        }
        try:
            async with httpx.AsyncClient(timeout=15, transport=self.transport) as http:
                short, _ = await self._token_request(
                    http, {**app, "redirect_uri": self.credential("redirect_uri"), "code": code}
                )
                token, expires_in = await self._token_request(
                    http, {**app, "grant_type": "fb_exchange_token", "fb_exchange_token": short}
                )
        except httpx.HTTPError:
            raise ReportError("unavailable", "Meta could not be reached.") from None
        return Grant(
            {"access_token": token}, identity=await self.user_name(token), expires_in=expires_in
        )

    async def user_name(self, access_token: str) -> str | None:
        """Who consented, for display. Best effort: None when Meta won't say."""
        try:
            async with self._client(timeout=15) as api:
                me = await self._get(api, access_token, "me", {"fields": "name"})
        except ReportError:
            return None
        name = me.get("name")
        return name if isinstance(name, str) else None

    async def list_ad_accounts(self, access_token: str) -> list[AdAccount]:
        """Every ad account the user can see (`ads_read` covers it)."""
        token = self._token(access_token)
        params = {"fields": "id,name,currency,account_status,timezone_name", "limit": 100}
        async with self._client() as api:
            return [
                AdAccount(
                    account=str(raw["id"]),
                    name=raw.get("name", ""),
                    currency=raw.get("currency", ""),
                    status=int(raw.get("account_status", 0) or 0),
                    timezone=raw.get("timezone_name", ""),
                )
                async for page in self._pages(api, token, "me/adaccounts", params)
                for raw in page
            ]

    async def list_accounts(self, credentials: Mapping[str, Any]) -> list[Account]:
        return [
            Account(
                id=a.account,
                name=a.name,
                currency=a.currency,
                timezone=a.timezone,
                details={"status": a.status},
            )
            for a in await self.list_ad_accounts(credentials.get("access_token"))  # type: ignore[arg-type]
        ]

    async def list_custom_conversions(
        self, access_token: str, ad_account: str
    ) -> list[CustomConversion]:
        """The custom conversions defined on the ad account (Meta has no catalog of insight
        fields: those are a fixed list, so this is the one per-account thing to discover)."""
        token, account = self._token(access_token), check_ad_account(ad_account)
        fields = "id,name,description,custom_event_type,default_conversion_value"
        params = {"fields": fields, "limit": 100}
        path = f"{account}/customconversions"
        async with self._client() as api:
            items = [item async for page in self._pages(api, token, path, params) for item in page]
        return parse_custom_conversions(items)

    async def read_insights(
        self,
        access_token: str,
        ad_account: str,
        *,
        fields: list[str],
        level: str,
        since: date,
        until: date,
        breakdowns: list[str] | None = None,
        time_increment: int = 1,
        day_column: str | None = "date_start",
    ) -> RawReport:
        """Insights over `since..until` (inclusive), as Meta returned the records."""
        token, account = self._token(access_token), check_ad_account(ad_account)
        time_range = {"since": since.isoformat(), "until": until.isoformat()}
        params: dict[str, Any] = {
            "fields": ",".join(fields),
            "level": level,
            "limit": PAGE_SIZE,
            "time_range": json.dumps(time_range, separators=(",", ":")),
            "time_increment": time_increment,
        }
        if breakdowns:
            params["breakdowns"] = ",".join(breakdowns)
        path = f"{account}/insights"
        async with self._client() as api:
            records = [r async for page in self._pages(api, token, path, params) for r in page]
        days = [parse_day(str(r.get(day_column) or "")) if day_column else None for r in records]
        _logger.info(f"Read {len(records)} {level} insights for {account}")
        return RawReport(records, days)

    async def read(
        self,
        credentials: Mapping[str, Any],
        account: str,
        spec: Mapping[str, Any],
        since: date,
        until: date,
    ) -> RawReport:
        return await self.read_insights(
            credentials.get("access_token"),  # type: ignore[arg-type]
            account,
            fields=list(spec["fields"]),
            level=spec["level"],
            breakdowns=spec.get("breakdowns"),
            time_increment=spec.get("time_increment", 1),
            since=since,
            until=until,
            day_column=spec.get("day_column", "date_start"),
        )
