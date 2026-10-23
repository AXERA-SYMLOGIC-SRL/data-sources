from __future__ import annotations

import asyncio
from typing import Any, Protocol

import httpx

from data_sources.core.exceptions import (
    AuthenticationError,
    DataSourceError,
    NotFoundError,
    RateLimitError,
)
from data_sources.core.logging import logger


class TokenSource(Protocol):
    async def get_token(self) -> str: ...

    async def close(self) -> None: ...


class GoogleAnalyticsClient:
    """Thin async wrapper around the GA4 Data API (v1beta) endpoints the connector needs:
    authentication, retry and URL mechanics only. Turning a report into rows, paging it
    and mapping it onto this SDK's types is the connector's job.
    """

    BASE_URL = "https://analyticsdata.googleapis.com/v1beta"
    ADMIN_URL = "https://analyticsadmin.googleapis.com/v1beta"

    def __init__(self, credential: TokenSource, http: httpx.AsyncClient | None = None) -> None:
        self.credential = credential
        self.logger = logger.getChild("google_analytics.client")
        self._http = http or httpx.AsyncClient()

    async def close(self) -> None:
        await self._http.aclose()
        await self.credential.close()
        self.logger.info("Google Analytics connection closed")

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            self.logger.error(f"Analytics API rejected credentials: {response.text}")
            raise AuthenticationError(f"Analytics API rejected credentials: {response.text}")
        if response.status_code == 404:
            self.logger.warning(f"Analytics API resource not found: {response.request.url}")
            raise NotFoundError(f"Analytics API resource not found: {response.request.url}")
        if response.status_code >= 400:
            self.logger.error(f"Analytics API error {response.status_code}: {response.text}")
            raise DataSourceError(f"Analytics API error {response.status_code}: {response.text}")

    async def _send(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        max_retries: int = 6,
    ) -> dict[str, Any]:
        """One call, retried while the API throttles (429, which is also how an exhausted
        property quota shows) or fails server-side, raising on any other error status."""
        for attempt in range(max_retries):
            response = await self._http.request(
                method,
                url,
                json=json,
                params=params,
                headers={"Authorization": f"Bearer {await self.credential.get_token()}"},
            )
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = float(response.headers.get("Retry-After", 2 ** min(attempt, 6)))
                self.logger.warning(
                    f"Analytics API throttled ({response.status_code}), retrying in {retry_after}s"
                )
                await asyncio.sleep(retry_after)
                continue
            self._raise_for_status(response)
            body: dict[str, Any] = response.json()
            return body

        raise RateLimitError(f"Analytics API throttled '{url}' after {max_retries} retries")

    async def run_report(self, property_name: str, body: dict[str, Any]) -> dict[str, Any]:
        """`properties/{id}:runReport` — one page of a report."""
        return await self._send("POST", f"{self.BASE_URL}/{property_name}:runReport", json=body)

    async def get_metadata(self, property_name: str) -> dict[str, Any]:
        """`properties/{id}/metadata` — the dimensions and metrics the property offers."""
        return await self._send("GET", f"{self.BASE_URL}/{property_name}/metadata")

    async def list_account_summaries(self) -> list[dict[str, Any]]:
        """Admin API `accountSummaries` — every account the credential can see, each with
        its property summaries — all pages."""
        summaries: list[dict[str, Any]] = []
        params: dict[str, Any] = {"pageSize": 200}
        while True:
            page = await self._send("GET", f"{self.ADMIN_URL}/accountSummaries", params=params)
            summaries.extend(page.get("accountSummaries", []))
            token = page.get("nextPageToken")
            if not token:
                return summaries
            params = {"pageSize": 200, "pageToken": token}
