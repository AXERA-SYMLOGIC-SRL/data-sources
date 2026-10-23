"""The Facebook Login steps that turn a user's consent into a token the connector can use.

Meta has no refresh tokens: the code from the consent buys a short-lived user token (about an
hour), which is exchanged for a long-lived one (about 60 days). When that one expires the
user has to consent again. A system-user token from Business Settings avoids this.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel

from data_sources.core.exceptions import AuthenticationError

DEFAULT_API_VERSION = "v26.0"


class AccessToken(BaseModel):
    token: str
    #: Seconds until it expires, when Meta says (a token that doesn't expire omits it).
    expires_in: int | None = None


def authorize_url(
    app_id: str,
    redirect_uri: str,
    state: str,
    scopes: tuple[str, ...] = ("ads_read",),
    api_version: str = DEFAULT_API_VERSION,
) -> str:
    """Where to send the user to consent."""
    query = urlencode(
        {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": ",".join(scopes),
            "response_type": "code",
        }
    )
    return f"https://www.facebook.com/{api_version}/dialog/oauth?{query}"


async def _get_token(
    http: httpx.AsyncClient, api_version: str, params: dict[str, str]
) -> AccessToken:
    response = await http.get(
        f"https://graph.facebook.com/{api_version}/oauth/access_token", params=params
    )
    body: dict[str, Any] = response.json() if response.content else {}
    if response.status_code >= 400 or "access_token" not in body:
        message = body.get("error", {}).get("message", response.text)
        raise AuthenticationError(f"Meta refused the token request: {message}")
    expires = body.get("expires_in")
    return AccessToken(
        token=str(body["access_token"]), expires_in=int(expires) if expires else None
    )


async def exchange_code(
    http: httpx.AsyncClient,
    app_id: str,
    app_secret: str,
    redirect_uri: str,
    code: str,
    api_version: str = DEFAULT_API_VERSION,
) -> AccessToken:
    """The short-lived user token the consent's `code` buys."""
    return await _get_token(
        http,
        api_version,
        {
            "client_id": app_id,
            "client_secret": app_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        },
    )


async def extend_token(
    http: httpx.AsyncClient,
    app_id: str,
    app_secret: str,
    token: str,
    api_version: str = DEFAULT_API_VERSION,
) -> AccessToken:
    """A long-lived (about 60 days) token for the short-lived user token `token`."""
    return await _get_token(
        http,
        api_version,
        {
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": token,
        },
    )
