"""What the Google report sources share: one OAuth "Web application" client (`client_id`,
`client_secret` and `redirect_uri` in `auth.credentials`) serving several products, each
consent asking for its own scope, and a refresh token per consent (`{"refresh_token"}` is the
grant's credentials)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

import httpx

from data_sources.reports.base import Grant, ReportSource
from data_sources.reports.errors import Problem
from data_sources.reports.http import ApiClient
from data_sources.reports.oauth import GoogleOAuth


def refresh_token_of(credentials: Mapping[str, Any]) -> str:
    """The grant's refresh token ("" when missing: the token request then fails as auth)."""
    value = credentials.get("refresh_token")
    return value if isinstance(value, str) else ""


class GoogleReportSource(ReportSource):
    """A Google product on the shared OAuth client. Not a source itself (it has no `provider`):
    the registry only picks up its concrete subclasses."""

    #: The API scope its consent asks for (`openid` and `email` are added).
    scope: ClassVar[str]
    #: What a refresh token Google won't trade for an access token raises.
    token_rejected: ClassVar[Problem] = Problem(
        "auth_failed", "Google did not return an access token."
    )
    app_credentials: ClassVar[tuple[str, ...]] = ("client_id", "client_secret")

    @property
    def oauth(self) -> GoogleOAuth:
        return GoogleOAuth(
            self.credential("client_id"),
            self.credential("client_secret"),
            self.credential("redirect_uri"),
        )

    def log_token_error(self, response: httpx.Response) -> None:
        """Sees a refused code exchange first (Google Ads logs redacted diagnostics)."""

    async def headers(self, api: ApiClient, refresh_token: object) -> dict[str, str]:
        """The request headers for `refresh_token` (override to authenticate otherwise, e.g.
        with a service account)."""
        token = await self.oauth.access_token(api, refresh_token, self.token_rejected)
        return {"Authorization": f"Bearer {token}"}

    def authorize_url(self, state: str) -> str:
        return self.oauth.authorize_url(self.scope, state)

    async def exchange_code(self, code: str) -> Grant:
        """`{"refresh_token"}`, and the consenting user's email as `identity` (when the
        `id_token` said)."""
        grant = await self.oauth.exchange_code(
            code, self.scope, transport=self.transport, on_error=self.log_token_error
        )
        return Grant({"refresh_token": grant.refresh_token}, identity=grant.email)
