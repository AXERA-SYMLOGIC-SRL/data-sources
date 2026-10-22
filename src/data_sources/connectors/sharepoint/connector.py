from __future__ import annotations

import builtins
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

from azure.identity.aio import ClientSecretCredential

from data_sources.config.schema import ConnectorConfig
from data_sources.connectors.sharepoint.client import SharepointClient
from data_sources.connectors.sharepoint.models import (
    SharePointItemRecord,
    SharePointSyncState,
    SharePointWebhookState,
)
from data_sources.core.connector import Connector
from data_sources.core.exceptions import (
    ConfigurationError,
    ConnectionError,
    DataSourceError,
    NotFoundError,
)
from data_sources.core.logging import logger
from data_sources.core.models import (
    Change,
    ChangeType,
    Hash,
    HashAlgorithm,
    Item,
    ItemType,
    Subscription,
    SyncCursor,
)
from data_sources.core.paths import is_excluded, join, relative_path
from data_sources.core.registry import register_connector

GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]

_HASH_FIELDS = (
    ("quickXorHash", HashAlgorithm.QUICK_XOR),
    ("sha1Hash", HashAlgorithm.SHA1),
    ("sha256Hash", HashAlgorithm.SHA256),
)

#: Graph's max subscription lifetime for drive/list resources (SharePoint document
#: libraries fall under "list" here, not the shorter driveItem limit).
_MAX_SUBSCRIPTION_LIFETIME = timedelta(days=30)


@register_connector("sharepoint")
class SharePointConnector(Connector):
    """Connector for a single SharePoint/OneDrive document library.

    Addressed via `config.options["url"]`, a SharePoint sharing link
    (`.../AllItems.aspx?id=/sites/.../Shared Documents/some/folder`) naming a drive and,
    optionally, a subfolder to scope the connector to. `config.options["excluded_paths"]`
    is an optional list of folders (relative to that folder) to skip, matched by whole
    path segments.

    That folder is a boundary: `get_metadata` and `download` refuse an item whose Graph
    path doesn't lie under it (`NotFoundError`), whatever id the caller passes, and
    `list(path)` takes `path` relative to it, refusing `..`.

    `list(recursive=True)` and `sync()` surface documents only — Graph's delta feed
    intermixes folder and file entries; `list(recursive=False)` browses one folder level
    and includes its subfolders.

    Every item `sync_in_background` sees is indexed locally, so a caller that only kept a
    `Change.item_id` can later call `get_item(item_id)` to recover the `Item` (and then
    `download(item)`) without a live Graph call.
    """

    provider = "sharepoint"
    supports_sync = True
    supports_item_lookup = True
    supports_webhooks = True
    models = (SharePointSyncState, SharePointItemRecord, SharePointWebhookState)

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
        self._root_item_id: str | None = None
        self._client: SharepointClient | None = None
        self.logger = logger.getChild("sharepoint.connector")

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
        root = await self._client.get_folder(self._drive_id, self._root_path)
        self._root_item_id = str(root["id"])

    async def validate(self) -> bool:
        try:
            await self.client.get_item_by_path(
                self._resolved_site_id, self._resolved_drive_id, self._root_path
            )
        except DataSourceError as exc:
            self.logger.warning(f"validate() failed for path {self._root_path!r}: {exc}")
            return False
        return True

    async def list(
        self, path: str | None = None, *, recursive: bool = False
    ) -> AsyncIterator[Item]:
        relative = relative_path(path)
        if is_excluded(relative, self._excluded_paths):
            return
        item_path = self._drive_path(relative)

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
                if "deleted" in raw or self._is_excluded(raw):
                    continue
                if "folder" in raw and is_excluded(
                    join(self._relative_path(raw), raw.get("name", "")), self._excluded_paths
                ):
                    continue
                yield self._to_item(raw)

    async def get_metadata(self, item_id: str) -> Item:
        """`item_id`'s metadata, refused (`NotFoundError`) unless it is the root folder or
        a live, non-excluded item whose Graph path lies under it."""
        raw = await self.client.get_item_by_id(self._resolved_drive_id, item_id)
        if self._root_item_id is not None and raw.get("id") == self._root_item_id:
            return self._to_item(raw)
        parent = self._parent_path(raw)
        if parent is None or "deleted" in raw or is_excluded(parent, self._excluded_paths):
            raise NotFoundError(f"{item_id!r} is not inside the connected folder")
        return self._to_item(raw)

    async def download(self, item: Item) -> AsyncIterator[bytes]:
        # Checked again by id, whatever `item` says: the caller may have kept it from before
        # the file moved, or built it.
        await self.get_metadata(item.id)
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

    @property
    def _webhook_resource(self) -> str:
        return f"/drives/{self._resolved_drive_id}/root"

    async def create_webhook(self, notification_url: str) -> Subscription:
        client_state = secrets.token_urlsafe(32)
        raw = await self.client.create_subscription(
            self._webhook_resource,
            notification_url,
            _default_expiration(),
            client_state,
        )
        subscription = _to_subscription(raw)
        await self._save_webhook_state(subscription.id, client_state)
        return subscription

    async def renew_webhook(self, subscription_id: str) -> Subscription:
        raw = await self.client.renew_subscription(subscription_id, _default_expiration())
        return _to_subscription(raw)

    async def delete_webhook(self, subscription_id: str) -> None:
        await self.client.delete_subscription(subscription_id)
        await self._clear_webhook_state(subscription_id)

    async def list_webhooks(self) -> builtins.list[Subscription]:
        resource = self._webhook_resource
        return [
            _to_subscription(raw)
            async for raw in self.client.list_subscriptions()
            if raw.get("resource") == resource
        ]

    async def verify_webhook_notification(self, payload: dict[str, Any]) -> bool:
        state = await self._load_webhook_state()
        if state is None:
            return False

        notifications = payload.get("value") or []
        if not notifications:
            return False

        return all(
            notification.get("subscriptionId") == state.subscription_id
            and notification.get("clientState") == state.client_state
            for notification in notifications
        )

    async def _load_webhook_state(self) -> SharePointWebhookState | None:
        assert self.store is not None
        async with self.store.session() as session:
            return await session.get(SharePointWebhookState, self._sync_key)

    async def _save_webhook_state(self, subscription_id: str, client_state: str) -> None:
        assert self.store is not None
        async with self.store.session() as session:
            state = await session.get(SharePointWebhookState, self._sync_key)
            if state is None:
                session.add(
                    SharePointWebhookState(
                        drive_key=self._sync_key,
                        subscription_id=subscription_id,
                        client_state=client_state,
                    )
                )
            else:
                state.subscription_id = subscription_id
                state.client_state = client_state
            await session.commit()

    async def _clear_webhook_state(self, subscription_id: str) -> None:
        assert self.store is not None
        async with self.store.session() as session:
            state = await session.get(SharePointWebhookState, self._sync_key)
            if state is not None and state.subscription_id == subscription_id:
                await session.delete(state)
                await session.commit()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    def _skip(self, raw: dict[str, Any]) -> bool:
        return "folder" in raw or "deleted" in raw or self._is_excluded(raw)

    def _drive_path(self, relative: str) -> str:
        """The drive path of the folder at `relative` (inside the root), as Graph's
        `root:/{path}:` addressing takes it."""
        if not relative:
            return self._root_path
        root = self._root_path.strip("/")
        return f"{root}/{relative}" if root else relative

    def _parent_path(self, raw: dict[str, Any]) -> str | None:
        """The item's parent folder relative to the root, or `None` when its Graph path
        doesn't lie under the root (or Graph didn't say where it is).

        Graph's `parentReference.path` looks like `/drives/{id}/root:/Reports/Q1/Archive` —
        a drive-internal prefix up to the literal `root:` segment, then the path from the
        drive's top. It's compared with the root's own path segment by segment (SharePoint
        paths are case-insensitive), never by the root folder's name alone, which another
        folder in the drive could share.
        """
        raw_path = raw.get("parentReference", {}).get("path")
        if not raw_path:
            return None
        _, anchor, after = unquote(str(raw_path)).partition("root:")
        if not anchor:
            return None
        segments = [segment for segment in after.split("/") if segment]
        root = [segment for segment in self._root_path.split("/") if segment]
        if [s.casefold() for s in segments[: len(root)]] != [s.casefold() for s in root]:
            return None
        return "/".join(segments[len(root) :])

    def _relative_path(self, raw: dict[str, Any]) -> str:
        """The parent path of an item listed or synced from under the root — inside by
        construction, so when Graph omits `parentReference.path` (delta responses may) it
        is taken to be the root itself."""
        return self._parent_path(raw) or ""

    def _is_excluded(self, raw: dict[str, Any]) -> bool:
        return is_excluded(self._relative_path(raw), self._excluded_paths)

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
            path=self._relative_path(raw),
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


def _default_expiration() -> datetime:
    return datetime.now(UTC) + _MAX_SUBSCRIPTION_LIFETIME


def _to_subscription(raw: dict[str, Any]) -> Subscription:
    return Subscription(
        id=raw["id"],
        resource=raw["resource"],
        notification_url=raw["notificationUrl"],
        expiration=datetime.fromisoformat(raw["expirationDateTime"].replace("Z", "+00:00")),
        client_state=raw.get("clientState"),
    )
