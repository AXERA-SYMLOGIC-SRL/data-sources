from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from azure.identity.aio import ClientSecretCredential

from data_sources.config.schema import ConnectorConfig
from data_sources.connectors.sharepoint.client import SharepointClient
from data_sources.connectors.sharepoint.models import SharePointItemRecord, SharePointSyncState
from data_sources.core.connector import Connector
from data_sources.core.exceptions import ConfigurationError, ConnectionError, DataSourceError
from data_sources.core.models import (
    Change,
    ChangeType,
    Hash,
    HashAlgorithm,
    Item,
    ItemType,
    SyncCursor,
)
from data_sources.core.registry import register_connector

GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]

_HASH_FIELDS = (
    ("quickXorHash", HashAlgorithm.QUICK_XOR),
    ("sha1Hash", HashAlgorithm.SHA1),
    ("sha256Hash", HashAlgorithm.SHA256),
)


@register_connector("sharepoint")
class SharePointConnector(Connector):
    """Connector for a single SharePoint/OneDrive document library.

    Addressed via `config.options["url"]`, a SharePoint sharing link
    (`.../AllItems.aspx?id=/sites/.../Shared Documents/some/folder`) naming a drive and,
    optionally, a subfolder to scope the connector to. `config.options["excluded_paths"]`
    is an optional list of path prefixes (relative to that folder) to skip.

    Folders are not surfaced as `Item`s — Graph's delta feed intermixes folder and file
    entries, but this connector only tracks documents.

    Every item `sync_in_background` sees is indexed locally, so a caller that only kept a
    `Change.item_id` can later call `get_item(item_id)` to recover the `Item` (and then
    `download(item)`) without a live Graph call.
    """

    provider = "sharepoint"
    supports_sync = True
    supports_item_lookup = True
    models = (SharePointSyncState, SharePointItemRecord)

    def __init__(self, config: ConnectorConfig) -> None:
        super().__init__(config)

        url = config.options.get("url")
        if not url:
            raise ConfigurationError("sharepoint connector requires options.url")

        self._url = url
        self._excluded_paths = tuple(config.options.get("excluded_paths", ()))
        self._drive_name, self._root_path = SharepointClient.parse_sharing_url(url)
        self._site_id: str | None = None
        self._drive_id: str | None = None
        self._client: SharepointClient | None = None

    @property
    def client(self) -> SharepointClient:
        if self._client is None:
            raise ConnectionError(
                f"{self.provider} connector is not connected; call connect() first"
            )
        return self._client

    @property
    def _sync_key(self) -> str:
        return self.config.name or self._url

    @property
    def _resolved_site_id(self) -> str:
        if self._site_id is None:
            raise ConnectionError(
                f"{self.provider} connector is not connected; call connect() first"
            )
        return self._site_id

    @property
    def _resolved_drive_id(self) -> str:
        if self._drive_id is None:
            raise ConnectionError(
                f"{self.provider} connector is not connected; call connect() first"
            )
        return self._drive_id

    async def connect(self) -> None:
        creds = (self.config.auth and self.config.auth.credentials) or {}
        missing = [key for key in ("tenant_id", "client_id", "client_secret") if key not in creds]
        if missing:
            raise ConfigurationError(
                f"sharepoint connector auth.credentials missing: {', '.join(missing)}"
            )

        credential = ClientSecretCredential(
            tenant_id=creds["tenant_id"],
            client_id=creds["client_id"],
            client_secret=creds["client_secret"],
        )
        self._client = SharepointClient(credential, GRAPH_SCOPES)
        self._site_id, self._drive_id = await self._client.resolve_drive(
            self._url, self._drive_name
        )

    async def validate(self) -> bool:
        try:
            await self.client.get_item_by_path(
                self._resolved_site_id, self._resolved_drive_id, self._root_path
            )
        except DataSourceError:
            return False
        return True

    async def list(
        self, path: str | None = None, *, recursive: bool = False
    ) -> AsyncIterator[Item]:
        item_path = path if path is not None else self._root_path

        if recursive:
            delta_url = self.client.delta_url(
                self._resolved_site_id, self._resolved_drive_id, item_path
            )
            async for raw, _ in self.client.get_delta(delta_url):
                if self._skip(raw):
                    continue
                yield self._to_item(raw)
        else:
            async for raw in self.client.list_children(self._resolved_drive_id, item_path):
                if self._skip(raw):
                    continue
                yield self._to_item(raw)

    async def get_metadata(self, item_id: str) -> Item:
        raw = await self.client.get_item_by_id(self._resolved_drive_id, item_id)
        return self._to_item(raw)

    async def download(self, item: Item) -> AsyncIterator[bytes]:
        async for chunk in self.client.download(self._resolved_drive_id, item.id):
            yield chunk

    async def sync(self, cursor: SyncCursor | None = None) -> AsyncIterator[Change]:
        delta_url = (
            cursor.token
            if cursor and cursor.token
            else self.client.delta_url(
                self._resolved_site_id, self._resolved_drive_id, self._root_path
            )
        )

        async for raw, delta_link in self.client.get_delta(delta_url):
            if "folder" in raw or self._is_excluded(raw):
                continue

            change_cursor = SyncCursor(token=delta_link) if delta_link else None

            if "deleted" in raw:
                yield Change(item_id=raw["id"], type=ChangeType.DELETED, cursor=change_cursor)
                continue

            change_type = (
                ChangeType.CREATED
                if raw.get("createdDateTime") == raw.get("lastModifiedDateTime")
                else ChangeType.UPDATED
            )
            yield Change(
                item_id=raw["id"],
                type=change_type,
                item=self._to_item(raw),
                timestamp=_parse_datetime(raw.get("lastModifiedDateTime")),
                cursor=change_cursor,
            )

    async def _load_cursor(self) -> SyncCursor | None:
        assert self.store is not None
        async with self.store.session() as session:
            state = await session.get(SharePointSyncState, self._sync_key)
            return SyncCursor(token=state.delta_link) if state else None

    async def _commit_cursor(self, cursor: SyncCursor) -> None:
        assert self.store is not None
        async with self.store.session() as session:
            state = await session.get(SharePointSyncState, self._sync_key)
            if state is None:
                session.add(
                    SharePointSyncState(drive_key=self._sync_key, delta_link=cursor.token or "")
                )
            else:
                state.delta_link = cursor.token or ""
            await session.commit()

    async def get_item(self, item_id: str) -> Item | None:
        assert self.store is not None
        async with self.store.session() as session:
            record = await session.get(SharePointItemRecord, (self._sync_key, item_id))
            return Item.model_validate_json(record.data) if record else None

    async def _save_item(self, item: Item) -> None:
        assert self.store is not None
        async with self.store.session() as session:
            record = await session.get(SharePointItemRecord, (self._sync_key, item.id))
            if record is None:
                session.add(
                    SharePointItemRecord(
                        drive_key=self._sync_key, item_id=item.id, data=item.model_dump_json()
                    )
                )
            else:
                record.data = item.model_dump_json()
            await session.commit()

    async def _delete_item(self, item_id: str) -> None:
        assert self.store is not None
        async with self.store.session() as session:
            record = await session.get(SharePointItemRecord, (self._sync_key, item_id))
            if record is not None:
                await session.delete(record)
                await session.commit()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    def _skip(self, raw: dict[str, Any]) -> bool:
        return "folder" in raw or "deleted" in raw or self._is_excluded(raw)

    @property
    def _root_anchor(self) -> str:
        """The last path segment Graph's `parentReference.path` uses for our root folder.

        Graph paths look like `/drives/{id}/root:/Reports/Q1/Archive` — never the drive's
        display name — so excluded-path matching anchors on the root folder's own name
        (or the literal `root:` segment when the connector is scoped to the drive root).
        """
        if self._root_path in ("", "/"):
            return "root:"
        return self._root_path.rstrip("/").rsplit("/", 1)[-1]

    def _is_excluded(self, raw: dict[str, Any]) -> bool:
        if not self._excluded_paths:
            return False
        segments = raw.get("parentReference", {}).get("path", "").split("/")
        anchor = self._root_anchor
        if anchor not in segments:
            return False
        relative = "/".join(segments[segments.index(anchor) + 1 :])
        return any(relative.startswith(excluded) for excluded in self._excluded_paths)

    def _to_item(self, raw: dict[str, Any]) -> Item:
        file_info = raw.get("file", {})
        file_hashes = file_info.get("hashes", {})
        hashes = [
            Hash(algorithm=algorithm, value=file_hashes[key])
            for key, algorithm in _HASH_FIELDS
            if key in file_hashes
        ]

        return Item(
            id=raw["id"],
            name=raw.get("name", ""),
            type=ItemType.FOLDER if "folder" in raw else ItemType.FILE,
            path=raw.get("parentReference", {}).get("path"),
            parent_id=raw.get("parentReference", {}).get("id"),
            size=raw.get("size"),
            mime_type=file_info.get("mimeType"),
            hashes=hashes,
            created_at=_parse_datetime(raw.get("createdDateTime")),
            modified_at=_parse_datetime(raw.get("lastModifiedDateTime")),
            metadata={"web_url": raw.get("webUrl")},
        )


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
