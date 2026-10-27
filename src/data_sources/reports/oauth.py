"""The OAuth steps report clients share: consent URLs, Google's code exchange and its refresh
token traded for an access token."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

from data_sources.reports.errors import Problem, ReportAuthError, ReportError
from data_sources.reports.http import ApiClient

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
OPENID = ("openid", "email")


def consent_url(base: str, **params: str) -> str:
    return f"{base}?{urlencode(params)}"


def email_from_id_token(id_token: object) -> str | None:
    """The email claim of an OpenID `id_token`. Not verified: the token came straight from the
    provider's token endpoint over TLS, and it is only shown as a label."""
    if not isinstance(id_token, str) or not id_token:
        return None
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return None
    email = claims.get("email") if isinstance(claims, dict) else None
    return email if isinstance(email, str) else None


def is_google_token_request(response: httpx.Response) -> bool:
    return str(response.request.url) == GOOGLE_TOKEN_URL


@dataclass(frozen=True)
class GoogleGrant:
    """What a consent bought: the refresh token, and the email of who gave it (when the
    `id_token` said)."""

    refresh_token: str = field(repr=False)
    email: str | None


@dataclass(frozen=True)
class GoogleOAuth:
    """A Google OAuth "Web application" client. One client can serve several products (Analytics,
    Ads, Search Console), each consent asking for its own `scope`."""

    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def authorize_url(self, scope: str, state: str) -> str:
        """Where to send the user to consent. `prompt=consent` makes Google return a refresh
        token every time, not only on the first consent."""
        return consent_url(
            GOOGLE_AUTH_URL,
            client_id=self.client_id,
            redirect_uri=self.redirect_uri,
            response_type="code",
            scope=" ".join((scope, *OPENID)),
            access_type="offline",
            prompt="consent",
            state=state,
        )

    async def exchange_code(
        self,
        code: str,
        scope: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        on_error: Callable[[httpx.Response], None] | None = None,
    ) -> GoogleGrant:
        """The refresh token (and the consenting user's email) the consent's `code` buys.
        `on_error` sees a refused response first (Google Ads logs redacted diagnostics)."""
        try:
            async with httpx.AsyncClient(timeout=15, transport=transport) as http:
                response = await http.post(
                    GOOGLE_TOKEN_URL,
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "redirect_uri": self.redirect_uri,
                    },
                )
        except httpx.HTTPError:
            raise ReportError("unavailable", "Google could not be reached.") from None
        if response.is_error and on_error is not None:
            on_error(response)
        if response.status_code == 429 or response.status_code >= 500:
            raise ReportError("unavailable", "Google is unavailable; try again.")
        if response.is_error:
            # Whatever the body (JSON or an HTML error page): Google refused the code.
            raise ReportAuthError("auth_failed", "Google refused the authorization code.")
        try:
            body = response.json() if response.content else {}
        except ValueError:
            raise ReportError("unavailable", "Google returned an invalid response.") from None
        if not isinstance(body, dict):
            raise ReportError("unavailable", "Google returned an invalid response.")
        refresh_token = body.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise ReportError("no_refresh_token", "Google returned no refresh token.")
        granted = body.get("scope")
        if "scope" in body and (not isinstance(granted, str) or scope not in granted.split()):
            raise ReportError("access_denied", "The consent didn't grant the access asked for.")
        return GoogleGrant(refresh_token, email_from_id_token(body.get("id_token")))

    async def access_token(self, api: ApiClient, refresh_token: object, rejected: Problem) -> str:
        """A fresh access token for `refresh_token`, through `api` (whose `classify` should treat
        an error from `GOOGLE_TOKEN_URL` as `rejected`)."""
        if not isinstance(refresh_token, str) or not refresh_token:
            raise ReportAuthError("auth_failed", "Authorize this source again.")
        if not self.configured:
            raise ReportError("oauth_not_configured", "Configure the Google OAuth client.")
        body = await api.json(
            "POST",
            GOOGLE_TOKEN_URL,
            retry=True,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise rejected.error()
        return token
