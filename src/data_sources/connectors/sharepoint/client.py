from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

import httpx

from data_sources.core.exceptions import (
    AuthenticationError,
    DataSourceError,
    NotFoundError,
    RateLimitError,
)
from data_sources.core.logging import logger


class GraphCredential(Protocol):
    async def get_token(self, *scopes: str) -> Any: ...

    async def close(self) -> None: ...


class SharepointClient:
    """Thin async wrapper around the Microsoft Graph endpoints a SharePoint/OneDrive
    connector needs: authentication, retry/pagination and Graph URL mechanics only.

    Mapping Graph payloads onto this SDK's `Item`/`Change` domain types, tracking sync
    cursors, and filtering by path are the connector's job, not the client's.
    """

    BASE_URL = "https://graph.microsoft.com/v1.0"

    def __init__(
        self,
        credential: GraphCredential,
        scopes: list[str],
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.credential = credential
        self.scopes = scopes
        self.logger = logger.getChild("sharepoint.client")
        self._http = http or httpx.AsyncClient()

    async def close(self) -> None:
        await self._http.aclose()
        await self.credential.close()
        self.logger.info("SharePoint connection closed")

    async def _auth_header(self) -> dict[str, str]:
        token = await self.credential.get_token(*self.scopes)
        return {"Authorization": f"Bearer {token.token}"}

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            self.logger.error(f"Graph API rejected credentials: {response.text}")
            raise AuthenticationError(f"Graph API rejected credentials: {response.text}")
        if response.status_code in (404, 410):
            self.logger.warning(f"Graph API item not found: {response.request.url}")
            raise NotFoundError(f"Graph API item not found: {response.request.url}")
        if response.status_code >= 400:
            self.logger.error(f"Graph API error {response.status_code}: {response.text}")
            raise DataSourceError(f"Graph API error {response.status_code}: {response.text}")

    async def get_json(self, url: str, max_retries: int = 10) -> dict[str, Any]:
        for attempt in range(max_retries):
            response = await self._http.get(url, headers=await self._auth_header())

            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", 2 ** min(attempt, 6)))
                self.logger.warning(f"Graph API throttled, retrying in {retry_after}s")
                await asyncio.sleep(retry_after)
                continue

            self._raise_for_status(response)
            body: dict[str, Any] = response.json()
            return body

        raise RateLimitError(f"Graph API throttled '{url}' after {max_retries} retries")

    async def _paginate(self, url: str) -> AsyncIterator[dict[str, Any]]:
        next_url: str | None = url
        while next_url:
            page = await self.get_json(next_url)
            for item in page.get("value", []):
                yield item
            next_url = page.get("@odata.nextLink")

    async def download(self, drive_id: str, item_id: str) -> AsyncIterator[bytes]:
        url = f"{self.BASE_URL}/drives/{drive_id}/items/{item_id}/content"
        async with self._http.stream(
            "GET", url, headers=await self._auth_header(), follow_redirects=True
        ) as response:
            self._raise_for_status(response)
            async for chunk in response.aiter_bytes():
                yield chunk

    async def get_site_id(self, site_url: str) -> str:
        parsed = urlparse(site_url)
        site_path = (
            "/".join(parsed.path.split("/")[:3]) if parsed.path and parsed.path != "/" else ""
        )
        result = await self.get_json(f"{self.BASE_URL}/sites/{parsed.netloc}:{site_path}")
        return str(result["id"])

    def get_drive_ids(self, site_id: str) -> AsyncIterator[dict[str, Any]]:
        return self._paginate(f"{self.BASE_URL}/sites/{site_id}/drives")

    def list_site_lists(self, site_id: str) -> AsyncIterator[dict[str, Any]]:
        return self._paginate(
            f"{self.BASE_URL}/sites/{site_id}/lists?$select=id,displayName,items,list,webUrl"
        )

    async def get_user_groups(self) -> list[str]:
        result = await self.get_json(
            f"{self.BASE_URL}/me/memberOf/microsoft.graph.group?$select=id"
        )
        return [group["id"] for group in result.get("value", [])]

    async def resolve_drive(self, site_url: str, drive_name: str) -> tuple[str, str]:
        """Resolve a site URL and a drive's display name to their Graph ids."""
        site_id = await self.get_site_id(site_url)
        async for drive in self.get_drive_ids(site_id):
            if unquote(drive["webUrl"].split("/")[-1]) == drive_name:
                self.logger.info(f"Connected to SharePoint drive {drive_name!r} at {site_url}")
                return site_id, drive["id"]
        self.logger.error(f"No drive named {drive_name!r} at site {site_url}")
        raise NotFoundError(f"No drive named {drive_name!r} at site {site_url}")

    async def get_item_by_id(self, drive_id: str, item_id: str) -> dict[str, Any]:
        return await self.get_json(f"{self.BASE_URL}/drives/{drive_id}/items/{item_id}")

    async def get_item_by_path(self, site_id: str, drive_id: str, item_path: str) -> dict[str, Any]:
        return await self.get_json(
            f"{self.BASE_URL}/sites/{site_id}/drives/{drive_id}/root:/{item_path}:/"
        )

    def list_children(self, drive_id: str, item_path: str) -> AsyncIterator[dict[str, Any]]:
        anchor = f"root:/{item_path}:" if item_path and item_path != "/" else "root"
        return self._paginate(f"{self.BASE_URL}/drives/{drive_id}/{anchor}/children")

    def delta_url(self, site_id: str, drive_id: str, item_path: str) -> str:
        return f"{self.BASE_URL}/sites/{site_id}/drives/{drive_id}/root:/{item_path}:/delta"

    async def get_delta(self, delta_url: str) -> AsyncIterator[tuple[dict[str, Any], str | None]]:
        """Walk a delta feed to completion, yielding `(raw_item, delta_link)` pairs.

        `delta_link` is `None` on every page except the last, where Graph swaps
        `@odata.nextLink` for `@odata.deltaLink` — the resume point for the next sync.
        """
        next_url: str | None = delta_url
        while next_url:
            page = await self.get_json(next_url)
            page_delta_link = page.get("@odata.deltaLink")
            for item in page.get("value", []):
                yield item, page_delta_link
            next_url = page.get("@odata.nextLink")

    async def create_subscription(
        self,
        resource: str,
        notification_url: str,
        expiration: datetime,
        client_state: str | None = None,
    ) -> dict[str, Any]:
        """Register a Graph subscription delivering change notifications for `resource`
        to `notification_url`.

        Graph validates `notification_url` synchronously before returning: it POSTs a
        `validationToken` there and requires it echoed back as plain text within 10
        seconds, so this call fails if that handshake isn't wired up on the receiving end.
        """
        body: dict[str, Any] = {
            "changeType": "updated",
            "resource": resource,
            "notificationUrl": notification_url,
            "expirationDateTime": self._format_expiration(expiration),
        }
        if client_state is not None:
            body["clientState"] = client_state

        response = await self._http.post(
            f"{self.BASE_URL}/subscriptions", json=body, headers=await self._auth_header()
        )
        self._raise_for_status(response)
        return dict(response.json())

    def list_subscriptions(self) -> AsyncIterator[dict[str, Any]]:
        return self._paginate(f"{self.BASE_URL}/subscriptions")

    async def renew_subscription(
        self, subscription_id: str, expiration: datetime
    ) -> dict[str, Any]:
        response = await self._http.patch(
            f"{self.BASE_URL}/subscriptions/{subscription_id}",
            json={"expirationDateTime": self._format_expiration(expiration)},
            headers=await self._auth_header(),
        )
        self._raise_for_status(response)
        return dict(response.json())

    async def delete_subscription(self, subscription_id: str) -> None:
        response = await self._http.delete(
            f"{self.BASE_URL}/subscriptions/{subscription_id}", headers=await self._auth_header()
        )
        self._raise_for_status(response)

    @staticmethod
    def _format_expiration(expiration: datetime) -> str:
        """Graph expects an ISO 8601 UTC timestamp; `Z` is the only suffix it accepts."""
        return expiration.astimezone(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def parse_sharing_url(url: str) -> tuple[str, str]:
        """Split a SharePoint `AllItems.aspx?id=...` sharing URL into `(drive_name, item_path)`."""
        queries = urlparse(url).query
        id_param = next((q for q in queries.split("&") if q.startswith("id=")), None)
        if id_param is None:
            raise ValueError(f"Not a SharePoint sharing URL (missing 'id=' query param): {url}")

        segments = unquote(id_param.removeprefix("id=")).split("/")
        drive_name = segments[3]
        item_path = "/".join(segments[4:]) or "/"
        return drive_name, item_path
