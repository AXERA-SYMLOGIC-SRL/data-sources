from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote, unquote, urlparse

import httpx

from data_sources.core.exceptions import (
    AuthenticationError,
    ConflictError,
    DataSourceError,
    NotFoundError,
    RateLimitError,
)
from data_sources.core.logging import logger

#: Content up to this size goes up in one request; above it, in an upload session.
SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024

#: One upload-session chunk — Graph needs a multiple of 320 KiB.
CHUNK_SIZE = 32 * 320 * 1024


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
        if response.status_code == 409:
            self.logger.warning(f"Graph API conflict: {response.text}")
            raise ConflictError(f"Graph API conflict: {response.text}")
        if response.status_code in (404, 410):
            self.logger.warning(f"Graph API item not found: {response.request.url}")
            raise NotFoundError(f"Graph API item not found: {response.request.url}")
        if response.status_code >= 400:
            self.logger.error(f"Graph API error {response.status_code}: {response.text}")
            raise DataSourceError(f"Graph API error {response.status_code}: {response.text}")

    async def send(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        authenticated: bool = True,
        max_retries: int = 10,
    ) -> httpx.Response:
        """One call to `url`, retried while Graph throttles, raising on an error status.
        `authenticated=False` for a pre-authorized URL (an upload session's), which must not
        be sent the access token."""
        for attempt in range(max_retries):
            auth = await self._auth_header() if authenticated else {}
            response = await self._http.request(
                method, url, json=json, content=content, headers={**(headers or {}), **auth}
            )

            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", 2 ** min(attempt, 6)))
                self.logger.warning(f"Graph API throttled, retrying in {retry_after}s")
                await asyncio.sleep(retry_after)
                continue

            self._raise_for_status(response)
            return response

        raise RateLimitError(f"Graph API throttled '{url}' after {max_retries} retries")

    async def get_json(self, url: str, max_retries: int = 10) -> dict[str, Any]:
        response = await self.send("GET", url, max_retries=max_retries)
        body: dict[str, Any] = response.json()
        return body

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

    @staticmethod
    def _anchor(item_path: str) -> str:
        """Graph's address for the folder at `item_path` — the drive's own `root` for none."""
        return f"root:/{quote(item_path.strip('/'))}:" if item_path.strip("/") else "root"

    async def get_folder(self, drive_id: str, item_path: str) -> dict[str, Any]:
        return await self.get_json(f"{self.BASE_URL}/drives/{drive_id}/{self._anchor(item_path)}")

    def path_url(self, drive_id: str, item_path: str) -> str:
        """Graph's address for whatever is at `item_path` in the drive, each segment encoded."""
        return f"{self.BASE_URL}/drives/{drive_id}/root:/{quote(item_path.strip('/'))}:"

    async def create_folder(self, drive_id: str, parent_path: str, name: str) -> dict[str, Any]:
        """Create the folder `name` in the one at `parent_path`; `ConflictError` when the
        name is taken."""
        response = await self.send(
            "POST",
            self.children_url(drive_id, parent_path),
            json={"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
        )
        return dict(response.json())

    async def upload(
        self, drive_id: str, item_path: str, content: bytes, mime_type: str, conflict: str
    ) -> dict[str, Any]:
        """Write `content` at `item_path` — in one request up to `SIMPLE_UPLOAD_LIMIT`, in an
        upload session above it; `conflict` is Graph's `fail`, `replace` or `rename`."""
        if len(content) <= SIMPLE_UPLOAD_LIMIT:
            response = await self.send(
                "PUT",
                f"{self.path_url(drive_id, item_path)}/content"
                f"?@microsoft.graph.conflictBehavior={conflict}",
                content=content,
                headers={"Content-Type": mime_type},
            )
            return dict(response.json())

        session = await self.send(
            "POST",
            f"{self.path_url(drive_id, item_path)}/createUploadSession",
            json={"item": {"@microsoft.graph.conflictBehavior": conflict}},
        )
        upload_url = str(session.json()["uploadUrl"])
        total = len(content)
        response = session
        for start in range(0, total, CHUNK_SIZE):
            chunk = content[start : start + CHUNK_SIZE]
            response = await self.send(
                "PUT",
                upload_url,
                content=chunk,
                headers={"Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{total}"},
                authenticated=False,
            )
        if response.status_code not in (200, 201):
            raise DataSourceError(f"Graph upload session ended with {response.status_code}")
        return dict(response.json())

    async def delete_item(self, drive_id: str, item_id: str, *, permanent: bool) -> None:
        """Delete `item_id` — to the site's recycle bin, unless `permanent`."""
        url = f"{self.BASE_URL}/drives/{drive_id}/items/{item_id}"
        if permanent:
            await self.send("POST", f"{url}/permanentDelete")
        else:
            await self.send("DELETE", url)

    def children_url(self, drive_id: str, item_path: str) -> str:
        return f"{self.BASE_URL}/drives/{drive_id}/{self._anchor(item_path)}/children"

    def list_children(self, drive_id: str, item_path: str) -> AsyncIterator[dict[str, Any]]:
        return self._paginate(self.children_url(drive_id, item_path))

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

    async def grant_site_permission(
        self, site_id: str, app_client_id: str, roles: list[str]
    ) -> dict[str, Any]:
        """Grant `app_client_id` the given `Sites.Selected` role(s) on `site_id`.

        This is a one-time, per-site call, meant to run with a *delegated* credential
        obtained from an interactive sign-in by that site's owner (or a SharePoint
        admin) — not with the app's own `ClientSecretCredential`, which is what every
        other method on this client is normally used with. Construct a throwaway
        `SharepointClient` wrapping that delegated token's credential just for this
        call; see docs/sharepoint.md for the full one-time-grant flow this supports.
        """
        body = {
            "roles": roles,
            "grantedToIdentities": [
                {"application": {"id": app_client_id, "displayName": app_client_id}}
            ],
        }
        response = await self._http.post(
            f"{self.BASE_URL}/sites/{site_id}/permissions",
            json=body,
            headers=await self._auth_header(),
        )
        self._raise_for_status(response)
        return dict(response.json())

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
