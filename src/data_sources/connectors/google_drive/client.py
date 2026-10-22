from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any, Protocol

import httpx

from data_sources.core.exceptions import (
    AuthenticationError,
    DataSourceError,
    NotFoundError,
    RateLimitError,
)
from data_sources.core.logging import logger

#: Every file field the connector reads — metadata, ancestry, hashes and shortcut target.
FILE_FIELDS = (
    "id,name,mimeType,parents,trashed,size,md5Checksum,sha1Checksum,sha256Checksum,"
    "createdTime,modifiedTime,webViewLink,driveId"
)

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
GOOGLE_APPS_PREFIX = "application/vnd.google-apps."

#: 403 reasons Drive uses for throttling — retried like a 429, unlike a real permission error.
_RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}


UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3"

#: Content up to this size goes up in one request; above it, in a resumable session.
SIMPLE_UPLOAD_LIMIT = 5 * 1024 * 1024

#: One resumable chunk — Drive needs a multiple of 256 KiB.
CHUNK_SIZE = 32 * 256 * 1024


def _json(response: httpx.Response) -> dict[str, Any]:
    if response.status_code == 204 or not response.content:
        return {}
    body: dict[str, Any] = response.json()
    return body


def query_literal(value: str) -> str:
    """A Drive query string literal, backslashes and quotes escaped."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


class TokenSource(Protocol):
    async def get_token(self) -> str: ...

    async def close(self) -> None: ...


class GoogleDriveClient:
    """Thin async wrapper around the Drive v3 endpoints a Google Drive connector needs:
    authentication, retry/pagination and Drive URL mechanics only.

    Mapping Drive payloads onto this SDK's `Item`/`Change` domain types, enforcing the
    connected folder's boundary and tracking sync cursors are the connector's job.
    """

    BASE_URL = "https://www.googleapis.com/drive/v3"

    def __init__(self, credential: TokenSource, http: httpx.AsyncClient | None = None) -> None:
        self.credential = credential
        self.logger = logger.getChild("google_drive.client")
        self._http = http or httpx.AsyncClient()

    async def close(self) -> None:
        await self._http.aclose()
        await self.credential.close()
        self.logger.info("Google Drive connection closed")

    async def _auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self.credential.get_token()}"}

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        if response.status_code == 429 or response.status_code >= 500:
            return True
        if response.status_code != 403:
            return False
        try:
            errors = response.json().get("error", {}).get("errors", [])
        except ValueError:
            return False
        return any(error.get("reason") in _RATE_LIMIT_REASONS for error in errors)

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            self.logger.error(f"Drive API rejected credentials: {response.text}")
            raise AuthenticationError(f"Drive API rejected credentials: {response.text}")
        if response.status_code in (404, 410):
            self.logger.warning(f"Drive API item not found: {response.request.url}")
            raise NotFoundError(f"Drive API item not found: {response.request.url}")
        if response.status_code >= 400:
            self.logger.error(f"Drive API error {response.status_code}: {response.text}")
            raise DataSourceError(f"Drive API error {response.status_code}: {response.text}")

    async def _send(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        max_retries: int = 10,
    ) -> httpx.Response:
        """One call to `url`, retried while Drive throttles, raising on an error status."""
        for attempt in range(max_retries):
            response = await self._http.request(
                method,
                url,
                params=params,
                json=json,
                content=content,
                headers={**(headers or {}), **await self._auth_header()},
            )

            if self._is_rate_limited(response):
                retry_after = float(response.headers.get("Retry-After", 2 ** min(attempt, 6)))
                self.logger.warning(
                    f"Drive API throttled ({response.status_code}), retrying in {retry_after}s"
                )
                await asyncio.sleep(retry_after)
                continue

            self._raise_for_status(response)
            return response

        raise RateLimitError(f"Drive API throttled '{url}' after {max_retries} retries")

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        max_retries: int = 10,
    ) -> dict[str, Any]:
        response = await self._send(
            method, f"{self.BASE_URL}/{path}", params=params, json=json, max_retries=max_retries
        )
        return _json(response)

    async def _paginate(
        self, path: str, params: dict[str, Any], key: str
    ) -> AsyncIterator[dict[str, Any]]:
        page_token: str | None = None
        while True:
            page = await self.request(
                "GET", path, params={**params, **({"pageToken": page_token} if page_token else {})}
            )
            for value in page.get(key, []):
                yield value
            page_token = page.get("nextPageToken")
            if not page_token:
                return

    async def get_file(self, file_id: str) -> dict[str, Any]:
        return await self.request(
            "GET", f"files/{file_id}", params={"fields": FILE_FIELDS, "supportsAllDrives": "true"}
        )

    def _list_params(self, query: str, drive_id: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "q": query,
            "fields": f"nextPageToken,files({FILE_FIELDS})",
            "pageSize": 1000,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if drive_id:
            params["corpora"] = "drive"
            params["driveId"] = drive_id
        return params

    def list_children(self, folder_id: str, drive_id: str | None) -> AsyncIterator[dict[str, Any]]:
        query = f"{query_literal(folder_id)} in parents and trashed = false"
        return self._paginate("files", self._list_params(query, drive_id), "files")

    async def children_page(
        self, folder_id: str, drive_id: str | None, page_size: int, page_token: str | None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One page of `folder_id`'s live children — folders first, then by name — and the
        token of the next page, if any."""
        query = f"{query_literal(folder_id)} in parents and trashed = false"
        params = {
            **self._list_params(query, drive_id),
            "pageSize": min(page_size, 1000),
            "orderBy": "folder,name",
        }
        if page_token:
            params["pageToken"] = page_token
        page = await self.request("GET", "files", params=params)
        next_token = page.get("nextPageToken")
        return list(page.get("files", [])), str(next_token) if next_token else None

    async def find_child_folders(
        self, parent_id: str, name: str, drive_id: str | None
    ) -> list[dict[str, Any]]:
        """The (at most two) live folders named exactly `name` directly inside `parent_id` —
        two meaning the name alone doesn't pick one."""
        return await self.find_children_named(parent_id, name, drive_id, folders=True)

    async def find_children_named(
        self, parent_id: str, name: str, drive_id: str | None, *, folders: bool | None
    ) -> list[dict[str, Any]]:
        """The (at most two) live children of `parent_id` named exactly `name` — folders
        only, other files only, or (`folders=None`) either."""
        query = f"name = {query_literal(name)} and {query_literal(parent_id)} in parents"
        if folders is not None:
            query += f" and mimeType {'=' if folders else '!='} '{FOLDER_MIME}'"
        query += " and trashed = false"
        page = await self.request(
            "GET", "files", params={**self._list_params(query, drive_id), "pageSize": 2}
        )
        return list(page.get("files", []))

    async def create_folder(self, parent_id: str, name: str) -> dict[str, Any]:
        return await self.request(
            "POST",
            "files",
            params={"fields": FILE_FIELDS, "supportsAllDrives": "true"},
            json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
        )

    async def create_file(
        self, parent_id: str, name: str, mime_type: str, content: bytes
    ) -> dict[str, Any]:
        return await self._upload(
            "POST", "files", {"name": name, "parents": [parent_id]}, mime_type, content
        )

    async def update_content(self, file_id: str, mime_type: str, content: bytes) -> dict[str, Any]:
        """New content for an existing file — the same file, its id and history kept."""
        return await self._upload("PATCH", f"files/{file_id}", None, mime_type, content)

    async def trash(self, file_id: str) -> None:
        await self.request(
            "PATCH",
            f"files/{file_id}",
            params={"supportsAllDrives": "true"},
            json={"trashed": True},
        )

    async def delete_file(self, file_id: str) -> None:
        await self.request("DELETE", f"files/{file_id}", params={"supportsAllDrives": "true"})

    async def _upload(
        self,
        method: str,
        path: str,
        metadata: dict[str, Any] | None,
        mime_type: str,
        content: bytes,
    ) -> dict[str, Any]:
        """Send `content` (with `metadata`, when creating) to the upload endpoint — in one
        request up to `SIMPLE_UPLOAD_LIMIT`, in a resumable session above it."""
        url = f"{UPLOAD_URL}/{path}"
        params: dict[str, Any] = {"fields": FILE_FIELDS, "supportsAllDrives": "true"}
        if len(content) > SIMPLE_UPLOAD_LIMIT:
            return await self._resumable(method, url, params, metadata, mime_type, content)
        if metadata is None:
            response = await self._send(
                method,
                url,
                params={**params, "uploadType": "media"},
                content=content,
                headers={"Content-Type": mime_type},
            )
            return _json(response)
        boundary = f"data-sources-{uuid.uuid4().hex}"
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
            + json.dumps(metadata).encode()
            + f"\r\n--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n".encode()
            + content
            + f"\r\n--{boundary}--".encode()
        )
        response = await self._send(
            method,
            url,
            params={**params, "uploadType": "multipart"},
            content=body,
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        )
        return _json(response)

    async def _resumable(
        self,
        method: str,
        url: str,
        params: dict[str, Any],
        metadata: dict[str, Any] | None,
        mime_type: str,
        content: bytes,
    ) -> dict[str, Any]:
        session = await self._send(
            method,
            url,
            params={**params, "uploadType": "resumable"},
            json=metadata or {},
            headers={
                "X-Upload-Content-Type": mime_type,
                "X-Upload-Content-Length": str(len(content)),
            },
        )
        session_url = session.headers["Location"]
        total = len(content)
        response = session
        for start in range(0, total, CHUNK_SIZE):
            chunk = content[start : start + CHUNK_SIZE]
            response = await self._send(
                "PUT",
                session_url,
                content=chunk,
                headers={"Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{total}"},
            )
        if response.status_code not in (200, 201):
            raise DataSourceError(f"Drive upload session ended with {response.status_code}")
        return _json(response)

    async def _stream(self, path: str, params: dict[str, Any]) -> AsyncIterator[bytes]:
        async with self._http.stream(
            "GET",
            f"{self.BASE_URL}/{path}",
            params=params,
            headers=await self._auth_header(),
            follow_redirects=True,
        ) as response:
            if response.status_code >= 400:
                await response.aread()
            self._raise_for_status(response)
            async for chunk in response.aiter_bytes():
                yield chunk

    def download(self, file_id: str) -> AsyncIterator[bytes]:
        return self._stream(f"files/{file_id}", {"alt": "media", "supportsAllDrives": "true"})

    def export(self, file_id: str, mime_type: str) -> AsyncIterator[bytes]:
        """A Google-native file (Doc, Sheet, Slides…) converted to `mime_type` — it has no
        bytes of its own to download. Drive caps exports at 10 MB."""
        return self._stream(f"files/{file_id}/export", {"mimeType": mime_type})

    async def get_start_page_token(self, drive_id: str | None) -> str:
        params: dict[str, Any] = {"supportsAllDrives": "true"}
        if drive_id:
            params["driveId"] = drive_id
        body = await self.request("GET", "changes/startPageToken", params=params)
        return str(body["startPageToken"])

    def _changes_params(self, page_token: str, drive_id: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "pageToken": page_token,
            "pageSize": 1000,
            "includeRemoved": "true",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "fields": (
                "nextPageToken,newStartPageToken,"
                f"changes(changeType,removed,fileId,time,file({FILE_FIELDS}))"
            ),
        }
        if drive_id:
            params["driveId"] = drive_id
        return params

    async def list_changes(
        self, page_token: str, drive_id: str | None
    ) -> AsyncIterator[tuple[list[dict[str, Any]], str]]:
        """Walk the change feed from `page_token` to its end, one page at a time, yielding
        `(changes, resume_token)` — `resume_token` being where to pick up once every change
        on that page is processed: the next page's token, or on the last page Drive's
        `newStartPageToken`, the resume point for the next sync.
        """
        token: str | None = page_token
        while token:
            page = await self.request(
                "GET", "changes", params=self._changes_params(token, drive_id)
            )
            next_token = page.get("nextPageToken")
            resume = next_token or page.get("newStartPageToken")
            if not resume:
                raise DataSourceError("Drive change page carried no token to resume from")
            yield list(page.get("changes", [])), str(resume)
            token = next_token

    async def watch_changes(
        self,
        page_token: str,
        drive_id: str | None,
        *,
        channel_id: str,
        address: str,
        token: str,
        expiration_ms: int,
    ) -> dict[str, Any]:
        """Open a notification channel on the change feed: Drive POSTs to `address` (an
        HTTPS URL) whenever something changes, echoing `token` in `X-Goog-Channel-Token`."""
        params: dict[str, Any] = {
            "pageToken": page_token,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if drive_id:
            params["driveId"] = drive_id
        return await self.request(
            "POST",
            "changes/watch",
            params=params,
            json={
                "id": channel_id,
                "type": "web_hook",
                "address": address,
                "token": token,
                "expiration": expiration_ms,
            },
        )

    async def stop_channel(self, channel_id: str, resource_id: str) -> None:
        await self.request(
            "POST", "channels/stop", json={"id": channel_id, "resourceId": resource_id}
        )

    def list_permissions(self, file_id: str) -> AsyncIterator[dict[str, Any]]:
        return self._paginate(
            f"files/{file_id}/permissions",
            {
                "fields": "nextPageToken,permissions(id,type,role,emailAddress,domain)",
                "supportsAllDrives": "true",
            },
            "permissions",
        )
