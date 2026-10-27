"""TikTok organic data through the Display API: Login Kit consent, the user's profile and counters,
and their public videos with theirs.

A personal account works (no Business or Creator account needed), but the API only gives what the
account has *now*: followers, likes and video count, and each video's views, likes, comments and
shares so far. There is no history and no audience data. A report is therefore a snapshot, labelled
with the day it was read; an application that reads it every day builds the history.

An access token lasts a day, so `refresh_grant` trades the refresh token (a year) for a new pair
before each read; TikTok may hand back a different refresh token, and the one it returns is the one
to keep. This is the Display API of TikTok for Developers (`client_key` / `client_secret`), not the
Marketing API of `tiktok_ads`: they are different apps on different portals.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

import httpx

from data_sources.core.logging import logger
from data_sources.reports.base import Account, Grant, ReportSource
from data_sources.reports.errors import Problem, ReportAuthError, ReportError
from data_sources.reports.http import ApiClient, Retry, Verdict
from data_sources.reports.models import RawReport
from data_sources.reports.oauth import consent_url

API_BASE = "https://open.tiktokapis.com/v2/"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
SCOPES = ("user.info.basic", "user.info.profile", "user.info.stats", "video.list")

#: By scope: `user.info.basic`, `.profile` and `.stats`.
USER_FIELDS = (
    "open_id",
    "union_id",
    "avatar_url",
    "display_name",
    "username",
    "bio_description",
    "profile_deep_link",
    "is_verified",
    "follower_count",
    "following_count",
    "likes_count",
    "video_count",
)
VIDEO_FIELDS = (
    "id",
    "create_time",
    "title",
    "video_description",
    "duration",
    "height",
    "width",
    "share_url",
    "embed_link",
    "cover_image_url",
    "like_count",
    "comment_count",
    "share_count",
    "view_count",
)
REPORTS = ("account", "videos")
#: Videos asked for per request (the API's maximum) and pages read before giving up.
PAGE_SIZE = 20
MAX_PAGES = 1000

#: Error codes (`error.code` in the body) that mean the token or the consent is no good.
AUTH_CODES = {
    "access_token_invalid",
    "access_token_expired",
    "scope_not_authorized",
    "scope_permission_missed",
    "user_not_authorized",
}
RETRY_CODES = {"rate_limit_exceeded", "internal_error", "service_unavailable"}

UNREACHABLE = Problem("unavailable", "TikTok could not be reached.")
BUSY = Problem("unavailable", "TikTok is temporarily unavailable or rate limited; retry later.")
INVALID = Problem("unavailable", "TikTok returned an invalid response.")
AUTH_FAILED = Problem(
    "auth_failed", "TikTok refused the token or a permission; authorize the account again."
)

_logger = logger.getChild("tiktok_organic.reports")


def _today() -> date:
    return datetime.now(UTC).date()


def _error(response: httpx.Response) -> tuple[str, str, str]:
    """`(code, message, log id)` of the body's `error` object, "" for what it doesn't say."""
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return "", "", ""
    if not isinstance(error, dict):
        return "", "", ""
    return tuple(  # type: ignore[return-value]
        " ".join(str(error.get(key) or "").split())[:300] for key in ("code", "message", "log_id")
    )


def classify(response: httpx.Response) -> Verdict:
    """TikTok answers `error.code: "ok"` on success; anything else is the error, with an HTTP
    status that agrees (429, 401...), so both are read."""
    code, message, log_id = _error(response)
    if response.status_code == 429 or response.status_code >= 500 or code in RETRY_CODES:
        return Retry(BUSY)
    if response.status_code in {401, 403} or code in AUTH_CODES:
        return AUTH_FAILED
    if response.is_error or code not in {"", "ok"}:
        detail = ": ".join(part for part in (code, message) if part)
        return Problem(
            "request_failed",
            "TikTok refused the request.",
            f"{detail} (log {log_id})" if log_id and detail else detail,
        )
    return None


class TikTokOrganicReports(ReportSource):
    """TikTok organic (`provider: tiktok_organic`): `client_key`, `client_secret` and
    `redirect_uri` in `auth.credentials`. The grant's credentials are `{"access_token",
    "refresh_token", "open_id", "scope"}`; the one account is the user (`open_id`); a report spec
    is `{"report": "account" | "videos"}`, read as of today (see the module's docstring)."""

    provider = "tiktok_organic"
    app_credentials = ("client_key", "client_secret")

    def _client(self, token: str | None = None) -> ApiClient:
        return ApiClient(
            classify,
            unreachable=UNREACHABLE,
            invalid=INVALID,
            base_url=API_BASE,
            headers={"Authorization": f"Bearer {token}"} if token else None,
            transport=self.transport,
            sleep=self.sleep,
        )

    def authorize_url(self, state: str) -> str:
        return consent_url(
            AUTH_URL,
            client_key=self.credential("client_key"),
            redirect_uri=self.credential("redirect_uri"),
            response_type="code",
            scope=",".join(SCOPES),
            state=state,
        )

    async def _token_request(self, params: dict[str, str]) -> dict[str, Any]:
        # Not through `classify`: the token endpoint answers `{"error": "invalid_grant", ...}`, not
        # the `{"error": {"code"}}` object of the rest of the API.
        try:
            async with httpx.AsyncClient(timeout=15, transport=self.transport) as http:
                response = await http.post(
                    f"{API_BASE}oauth/token/",
                    data={
                        "client_key": self.credential("client_key"),
                        "client_secret": self.credential("client_secret"),
                        **params,
                    },
                )
        except httpx.HTTPError:
            raise ReportError("unavailable", "TikTok could not be reached.") from None
        if response.status_code == 429 or response.status_code >= 500:
            raise ReportError("unavailable", "TikTok is unavailable; try again.")
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise INVALID.error()
        token, refresh = body.get("access_token"), body.get("refresh_token")
        if response.is_error or body.get("error") or not token or not refresh:
            reason = " ".join(str(body.get("error_description") or body.get("error") or "").split())
            raise ReportAuthError(
                "auth_failed", "TikTok refused the code or the refresh token.", reason[:300]
            )
        return body

    @staticmethod
    def _grant(body: dict[str, Any]) -> Grant:
        scope = body.get("scope")
        granted = {s for s in str(scope or "").split(",") if s}
        if not granted >= set(SCOPES):
            missing = ", ".join(s for s in SCOPES if s not in granted)
            raise ReportError(
                "access_denied", f"The consent left out permissions the reports need: {missing}."
            )
        expires = body.get("refresh_expires_in")
        return Grant(
            {
                "access_token": str(body["access_token"]),
                "refresh_token": str(body["refresh_token"]),
                "open_id": str(body.get("open_id") or ""),
                "scope": ",".join(sorted(granted)),
            },
            expires_in=expires if isinstance(expires, int) and expires > 0 else None,
        )

    async def exchange_code(self, code: str) -> Grant:
        """The tokens the consent's `code` buys. The consent may leave permissions out: that is
        `access_denied`, since the reports need all of them."""
        body = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.credential("redirect_uri"),
            }
        )
        return self._grant(body)

    async def refresh_grant(self, credentials: Mapping[str, Any]) -> Grant:
        """A fresh access token (and the refresh token TikTok says to keep from now on) for a
        grant whose access token has expired. Fails with `auth_failed` once the refresh token has
        too: the user has to consent again."""
        refresh = credentials.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            raise AUTH_FAILED.error()
        body = await self._token_request({"grant_type": "refresh_token", "refresh_token": refresh})
        # A refresh answers with the new tokens; who and what they are for is what was stored.
        merged = {"open_id": credentials.get("open_id"), "scope": credentials.get("scope"), **body}
        return self._grant(merged)

    @staticmethod
    def _token(credentials: Mapping[str, Any]) -> str:
        token = credentials.get("access_token")
        if not isinstance(token, str) or not token:
            raise AUTH_FAILED.error()
        return token

    async def _user(self, api: ApiClient) -> dict[str, Any]:
        body = await api.json("GET", "user/info/", params={"fields": ",".join(USER_FIELDS)})
        data = body.get("data")
        user = data.get("user") if isinstance(data, dict) else None
        if not isinstance(user, dict) or not user.get("open_id"):
            raise INVALID.error()
        return user

    async def list_accounts(self, credentials: Mapping[str, Any]) -> list[Account]:
        """The consenting user: a grant reads one account."""
        async with self._client(self._token(credentials)) as api:
            user = await self._user(api)
        name = str(user.get("display_name") or user.get("username") or user["open_id"])
        return [
            Account(
                id=str(user["open_id"]),
                name=name,
                details={"username": user.get("username"), "verified": user.get("is_verified")},
            )
        ]

    async def _videos(self, api: ApiClient) -> list[dict[str, Any]]:
        videos: list[dict[str, Any]] = []
        cursor: int | None = None
        for _ in range(MAX_PAGES):
            payload: dict[str, Any] = {"max_count": PAGE_SIZE}
            if cursor is not None:
                payload["cursor"] = cursor
            body = await api.json(
                "POST",
                "video/list/",
                params={"fields": ",".join(VIDEO_FIELDS)},
                json=payload,
                retry=True,  # a listing, safe to repeat
            )
            data = body.get("data")
            page = data.get("videos") if isinstance(data, dict) else None
            if page is None and isinstance(data, dict):
                page = []  # no videos: TikTok leaves the key out
            if not isinstance(page, list) or any(
                not isinstance(v, dict) or not v.get("id") for v in page
            ):
                raise INVALID.error()
            videos.extend(page)
            more, after = data.get("has_more"), data.get("cursor")
            if not more:
                return videos
            if not isinstance(after, int) or isinstance(after, bool) or after == cursor:
                raise INVALID.error()  # a cursor that doesn't move would page forever
            cursor = after
        raise INVALID.error()

    async def read(
        self,
        credentials: Mapping[str, Any],
        account: str,
        spec: Mapping[str, Any],
        since: date,
        until: date,
    ) -> RawReport:
        """The `spec["report"]` as it is now, every record labelled with today (`since` and
        `until` don't apply: the API has no history)."""
        report = spec.get("report")
        if report not in REPORTS:
            raise ReportError("request_failed", "Unsupported TikTok organic report.")
        if account != credentials.get("open_id"):
            raise ReportAuthError("auth_failed", "This account was not authorized for the token.")
        today = _today()
        async with self._client(self._token(credentials)) as api:
            if report == "account":
                records = [await self._user(api)]
            else:
                records = await self._videos(api)
        _logger.info(f"Read {len(records)} {report} records for {account}")
        return RawReport(records, [today] * len(records), {"open_id": account, "report": report})
