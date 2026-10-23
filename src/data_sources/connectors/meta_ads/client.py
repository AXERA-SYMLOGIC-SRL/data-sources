from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from data_sources.connectors.meta_ads.oauth import DEFAULT_API_VERSION
from data_sources.core.exceptions import (
    AuthenticationError,
    DataSourceError,
    NotFoundError,
    RateLimitError,
)
from data_sources.core.logging import logger

#: Graph error codes that mean "slow down": app-level (4, 17, 32, 341, 613) and the
#: ad-account ones (80000-80014 are the Business Use Case limits).
_THROTTLE_CODES = {4, 17, 32, 341, 613, *range(80000, 80015)}

#: Codes for a token that is invalid, expired, or lacks the permission (190, 102: bad or
#: expired token; 10 and 200-299: permission missing).
_AUTH_CODES = {102, 190, 10, *range(200, 300)}

#: A throttle that Meta says lasts longer than this raises instead of sleeping.
MAX_THROTTLE_WAIT = 120.0


def _error(response: httpx.Response) -> tuple[int | None, str]:
    try:
        error = response.json()["error"]
        return error.get("code"), str(error.get("message", response.text))
    except (ValueError, KeyError, TypeError, AttributeError):
        return None, response.text


def _regain_seconds(response: httpx.Response) -> float | None:
    """How long Meta says the ad account is blocked (`x-business-use-case-usage`), if it
    says."""
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


class MetaAdsClient:
    """Thin async wrapper around the Graph API calls the Meta Ads connector needs: the token
    (as a bearer header, with `appsecret_proof` when the app secret is known), cursor paging,
    throttling retry and error mapping. Turning insights into rows is the connector's job.
    """

    def __init__(
        self,
        access_token: str,
        *,
        app_secret: str | None = None,
        api_version: str = DEFAULT_API_VERSION,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = access_token
        self._proof = (
            hmac.new(app_secret.encode(), access_token.encode(), hashlib.sha256).hexdigest()
            if app_secret
            else None
        )
        self.base_url = f"https://graph.facebook.com/{api_version}"
        self.logger = logger.getChild("meta_ads.client")
        self._http = http or httpx.AsyncClient(timeout=60)

    async def close(self) -> None:
        await self._http.aclose()
        self.logger.info("Meta Ads connection closed")

    def _raise_for_status(self, response: httpx.Response) -> None:
        code, message = _error(response)
        if code in _AUTH_CODES or response.status_code in (401, 403):
            self.logger.error(f"Meta API rejected the token: {message}")
            raise AuthenticationError(f"Meta API rejected the token: {message}")
        if response.status_code == 404 or code == 803:
            raise NotFoundError(f"Meta API object not found: {message}")
        self.logger.error(f"Meta API error {response.status_code}: {message}")
        raise DataSourceError(f"Meta API error {response.status_code} (code {code}): {message}")

    async def get(
        self, path: str, params: dict[str, Any] | None = None, max_retries: int = 5
    ) -> dict[str, Any]:
        """GET `path`, retried while throttled or failing server-side; raising on any other
        error."""
        query = dict(params or {})
        if self._proof:
            query["appsecret_proof"] = self._proof
        for attempt in range(max_retries):
            response = await self._http.get(
                f"{self.base_url}/{path}",
                params=query,
                headers={"Authorization": f"Bearer {self._token}"},
            )
            if response.status_code < 400:
                body: dict[str, Any] = response.json()
                return body

            code, message = _error(response)
            if code in _THROTTLE_CODES or response.status_code >= 500:
                wait = _regain_seconds(response) or 2 ** min(attempt + 1, 6)
                if wait > MAX_THROTTLE_WAIT:
                    raise RateLimitError(
                        f"Meta API throttled this ad account for about {wait / 60:.0f} "
                        f"minutes: {message}"
                    )
                self.logger.warning(f"Meta API throttled ({code}), retrying in {wait}s")
                await asyncio.sleep(wait)
                continue
            self._raise_for_status(response)

        raise RateLimitError(f"Meta API throttled '{path}' after {max_retries} retries")

    async def get_pages(
        self, path: str, params: dict[str, Any]
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Yield each page of `data` from a cursor-paged edge."""
        query = dict(params)
        while True:
            page = await self.get(path, query)
            yield page.get("data", [])
            after = page.get("paging", {}).get("cursors", {}).get("after")
            if not after or "next" not in page.get("paging", {}):
                return
            query["after"] = after
