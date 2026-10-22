from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx
from google.auth import crypt, jwt

from data_sources.config.schema import AuthConfig
from data_sources.core.exceptions import AuthenticationError, ConfigurationError
from data_sources.core.logging import logger

TOKEN_URL = "https://oauth2.googleapis.com/token"

#: What a service account asks for when `auth.credentials.scopes` doesn't say otherwise.
#: An OAuth refresh token carries the scopes it was consented with, so it sends none.
DEFAULT_SCOPES = ("https://www.googleapis.com/auth/drive.readonly",)

#: Refresh this long before Google says the access token expires, so a request started
#: just before expiry doesn't go out with a token that dies in flight.
_EXPIRY_MARGIN_SECONDS = 60

_logger = logger.getChild("google_drive.auth")


class DriveCredential(ABC):
    """Hands out a valid Google access token, fetching a new one when the cached one is
    about to expire. Subclasses only say how a new token is obtained."""

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http or httpx.AsyncClient()
        self._token: str | None = None
        self._expires_at = 0.0

    async def get_token(self) -> str:
        if self._token is None or time.time() >= self._expires_at - _EXPIRY_MARGIN_SECONDS:
            self._token, expires_in = await self._request_token()
            self._expires_at = time.time() + expires_in
        return self._token

    async def close(self) -> None:
        await self._http.aclose()

    @abstractmethod
    async def _request_token(self) -> tuple[str, float]:
        """Fetch a fresh access token: `(token, seconds until it expires)`."""

    async def _post_token_request(self, data: dict[str, str]) -> tuple[str, float]:
        response = await self._http.post(TOKEN_URL, data=data)
        if response.status_code >= 400:
            _logger.error(f"Google token endpoint refused the credentials: {response.text}")
            raise AuthenticationError(
                f"Google token endpoint refused the credentials: {response.text}"
            )
        body: dict[str, Any] = response.json()
        return str(body["access_token"]), float(body.get("expires_in", 3600))


class RefreshTokenCredential(DriveCredential):
    """A user's OAuth consent, kept as its refresh token — what a "Connect Google" flow
    yields. Acts as that user, within the scopes they consented to."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(http)
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token

    async def _request_token(self) -> tuple[str, float]:
        return await self._post_token_request(
            {
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
            }
        )


class ServiceAccountCredential(DriveCredential):
    """A service account's key: sees what is shared with its own email address (or a
    shared drive it is a member of) — or, given `subject`, acts as that Workspace user
    through domain-wide delegation."""

    def __init__(
        self,
        info: dict[str, Any],
        scopes: tuple[str, ...] = DEFAULT_SCOPES,
        subject: str | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(http)
        try:
            self._signer = crypt.RSASigner.from_service_account_info(info)  # type: ignore[no-untyped-call]
            self._email = str(info["client_email"])
        except (KeyError, ValueError) as exc:
            raise ConfigurationError(f"invalid Google service account key: {exc}") from exc
        self._token_uri = str(info.get("token_uri") or TOKEN_URL)
        self._scopes = scopes
        self._subject = subject

    async def _request_token(self) -> tuple[str, float]:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self._email,
            "scope": " ".join(self._scopes),
            "aud": self._token_uri,
            "iat": now,
            "exp": now + 3600,
        }
        if self._subject:
            claims["sub"] = self._subject
        assertion = jwt.encode(self._signer, claims).decode()  # type: ignore[no-untyped-call]
        return await self._post_token_request(
            {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}
        )


def credential_from_config(auth: AuthConfig | None) -> DriveCredential:
    """The credential `auth` describes — `type="oauth"` (`client_id`, `client_secret`,
    `refresh_token`) or `type="service_account"` (`service_account_info` as a dict or its
    JSON text, optional `subject` and `scopes`)."""
    if auth is None:
        raise ConfigurationError("google_drive connector requires auth")
    creds = auth.credentials

    if auth.type == "oauth":
        missing = [k for k in ("client_id", "client_secret", "refresh_token") if not creds.get(k)]
        if missing:
            raise ConfigurationError(
                f"google_drive connector auth.credentials missing: {', '.join(missing)}"
            )
        return RefreshTokenCredential(
            creds["client_id"], creds["client_secret"], creds["refresh_token"]
        )

    if auth.type == "service_account":
        info = creds.get("service_account_info")
        if isinstance(info, str):
            try:
                info = json.loads(info)
            except ValueError as exc:
                raise ConfigurationError(
                    "google_drive auth.credentials.service_account_info is not valid JSON"
                ) from exc
        if not isinstance(info, dict):
            raise ConfigurationError(
                "google_drive connector auth.credentials missing: service_account_info"
            )
        scopes = tuple(creds.get("scopes") or DEFAULT_SCOPES)
        return ServiceAccountCredential(info, scopes, creds.get("subject"))

    raise ConfigurationError(
        f"google_drive connector auth.type must be 'oauth' or 'service_account', got {auth.type!r}"
    )
