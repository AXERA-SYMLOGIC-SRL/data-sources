from __future__ import annotations

import builtins
import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from data_sources.config.schema import ConnectorConfig
from data_sources.connectors.google_drive.auth import credential_from_config
from data_sources.connectors.google_drive.client import (
    FOLDER_MIME,
    SHORTCUT_MIME,
    GoogleDriveClient,
)
from data_sources.connectors.google_drive.items import (
    export_formats,
    is_document,
    to_item,
)
from data_sources.connectors.google_drive.models import (
    GoogleDriveFolderRecord,
    GoogleDriveItemRecord,
    GoogleDriveSyncState,
    GoogleDriveWebhookState,
)
from data_sources.connectors.google_drive.scope import (
    Ancestry,
    parse_folder,
    resolve_path,
    walk,
)
from data_sources.connectors.google_drive.state import DriveState
from data_sources.connectors.google_drive.sync import DriveSync
from data_sources.core.connector import Connector
from data_sources.core.exceptions import (
    ConfigurationError,
    ConnectionError,
    DataSourceError,
    NotFoundError,
    UnsupportedOperationError,
)
from data_sources.core.logging import logger
from data_sources.core.models import (
    Change,
    Item,
    Permission,
    PermissionPrincipalType,
    PermissionRole,
    Subscription,
    SyncCursor,
)
from data_sources.core.paths import is_excluded, relative_path
from data_sources.core.registry import register_connector

#: Drive's longest lifetime for a channel on the change feed.
_MAX_CHANNEL_LIFETIME = timedelta(days=7)

_ROLES = {
    "owner": PermissionRole.OWNER,
    "organizer": PermissionRole.OWNER,
    "fileOrganizer": PermissionRole.WRITER,
    "writer": PermissionRole.WRITER,
    "commenter": PermissionRole.COMMENTER,
    "reader": PermissionRole.READER,
}

_PRINCIPAL_TYPES = {
    "user": PermissionPrincipalType.USER,
    "group": PermissionPrincipalType.GROUP,
    "domain": PermissionPrincipalType.DOMAIN,
    "anyone": PermissionPrincipalType.ANYONE,
}


@register_connector("google_drive")
class GoogleDriveConnector(Connector):
    """Connector for one Google Drive folder and everything below it — in My Drive, shared
    with the account, or in a shared drive.

    Addressed via `config.options["folder"]`: the folder's id or URL, or `root` for the whole
    of My Drive (never a folder name — a name isn't one folder in a Drive).
    `options.excluded_paths` lists folders (relative to it) to skip, and
    `options.export_formats` overrides what Google-native files are downloaded as (see
    `items.DEFAULT_EXPORTS`). Auth is `type="oauth"` (a user's refresh token) or
    `type="service_account"` — see `auth.credential_from_config`.

    The folder is a boundary: `get_metadata`, `download` and `get_permissions` refuse an item
    whose current ancestry doesn't reach it (`NotFoundError`, so nothing outside is
    revealed), whatever id the caller passes. Shortcuts are never followed, and Google-native
    files with no export format (Forms, Sites…) are not listed.

    `list(recursive=True)` and `sync()` surface files only; `list(recursive=False)` browses
    one folder level and includes its subfolders.

    `sync()` walks the folder the first time and reads Drive's change feed after that (see
    `sync.DriveSync`); every item `sync_in_background` sees is indexed, so a later
    `get_item(item_id)` resolves it without a live call.
    """

    provider = "google_drive"
    supports_sync = True
    supports_item_lookup = True
    supports_webhooks = True
    supports_permissions = True
    models = (
        GoogleDriveSyncState,
        GoogleDriveItemRecord,
        GoogleDriveFolderRecord,
        GoogleDriveWebhookState,
    )

    def __init__(self, config: ConnectorConfig) -> None:
        super().__init__(config)

        folder = config.options.get("folder")
        if not folder:
            raise ConfigurationError("google_drive connector requires options.folder")
        folder_id = parse_folder(str(folder))
        if folder_id is None:
            raise ConfigurationError(
                f"google_drive options.folder must be a Drive folder id or URL, got {folder!r}"
            )

        self._folder = str(folder)
        self._configured_root = folder_id
        self._excluded_paths = tuple(config.options.get("excluded_paths", ()))
        self._exports = export_formats(config.options.get("export_formats"))
        self._root_id: str | None = None
        self._drive_id: str | None = None
        self._client: GoogleDriveClient | None = None
        self._sync: DriveSync | None = None
        self.logger = logger.getChild("google_drive.connector")

    @property
    def client(self) -> GoogleDriveClient:
        if self._client is None:
            raise ConnectionError(
                f"{self.provider} connector is not connected; call connect() first"
            )
        return self._client

    @property
    def _root(self) -> str:
        if self._root_id is None:
            raise ConnectionError(
                f"{self.provider} connector is not connected; call connect() first"
            )
        return self._root_id

    @property
    def _sync_key(self) -> str:
        return self.config.name or self._folder

    @property
    def _state(self) -> DriveState:
        assert self.store is not None
        return DriveState(self.store, self._sync_key)

    async def connect(self) -> None:
        self._client = GoogleDriveClient(credential_from_config(self.config.auth))
        try:
            await self._bind_root()
        except BaseException:
            await self._client.close()
            self._client = None
            raise

    async def _bind_root(self) -> None:
        """Resolve the configured folder to the id its files' `parents` name (`root` is only
        an alias), and the shared drive it's on, if any."""
        root = await self.client.get_file(self._configured_root)
        if root.get("trashed") or root.get("mimeType") != FOLDER_MIME:
            raise ConfigurationError(
                f"google_drive options.folder {self._folder!r} is not a folder"
            )
        self._root_id = str(root["id"])
        self._drive_id = root.get("driveId")
        self.logger.info(f"Connected to Google Drive folder {root.get('name')!r}")

    async def validate(self) -> bool:
        try:
            root = await self.client.get_file(self._root)
        except DataSourceError as exc:
            self.logger.warning(f"validate() failed for folder {self._folder!r}: {exc}")
            return False
        if root.get("trashed") or root.get("mimeType") != FOLDER_MIME:
            self.logger.warning(f"validate() failed: {self._folder!r} is no longer a folder")
            return False
        return True

    async def list(
        self, path: str | None = None, *, recursive: bool = False
    ) -> AsyncIterator[Item]:
        start_path = relative_path(path)
        if is_excluded(start_path, self._excluded_paths):
            return
        folder_id = (
            await resolve_path(self.client, self._root, start_path, self._drive_id)
            if start_path
            else self._root
        )

        if recursive:
            async for raw, parent_path in walk(
                self.client, folder_id, start_path, self._drive_id, self._excluded_paths
            ):
                if is_document(raw, self._exports):
                    yield to_item(raw, parent_path, self._exports)
            return

        async for raw in self.client.list_children(folder_id, self._drive_id):
            mime = raw.get("mimeType")
            if mime == SHORTCUT_MIME:
                continue
            if mime == FOLDER_MIME:
                folder_path = f"{start_path}/{raw.get('name', '')}".lstrip("/")
                if not is_excluded(folder_path, self._excluded_paths):
                    yield to_item(raw, start_path, self._exports)
            elif is_document(raw, self._exports):
                yield to_item(raw, start_path, self._exports)

    async def get_metadata(self, item_id: str) -> Item:
        raw, parent_path = await self._inside(item_id)
        return to_item(raw, parent_path, self._exports)

    async def download(self, item: Item) -> AsyncIterator[bytes]:
        # Checked again by id, whatever `item` says: the caller may have kept it from before
        # the file moved, or built it.
        raw, _ = await self._inside(item.id)
        mime = str(raw.get("mimeType", ""))
        if mime == FOLDER_MIME or not is_document(raw, self._exports):
            raise UnsupportedOperationError(f"{item.id!r} ({mime}) has no content to download")
        export = self._exports.get(mime)
        chunks = (
            self.client.export(item.id, export[0])
            if export is not None
            else self.client.download(item.id)
        )
        async for chunk in chunks:
            yield chunk

    async def _inside(self, item_id: str) -> tuple[dict[str, Any], str]:
        """`item_id`'s metadata and parent path, refused unless it is the root or a live,
        non-excluded item whose current ancestry reaches it."""
        raw = await self.client.get_file(item_id)
        if str(raw.get("id")) == self._root:
            return raw, ""
        if raw.get("trashed") or raw.get("mimeType") == SHORTCUT_MIME:
            raise NotFoundError(f"{item_id!r} is not inside the connected folder")
        parent_path = await Ancestry(self.client, self._root).parent_path(raw)
        if parent_path is None or is_excluded(parent_path, self._excluded_paths):
            raise NotFoundError(f"{item_id!r} is not inside the connected folder")
        return raw, parent_path

    def sync(self, cursor: SyncCursor | None = None) -> AsyncIterator[Change]:
        self._sync = DriveSync(
            self.client,
            self._root,
            self._drive_id,
            self._excluded_paths,
            self._exports,
            self._state if self.store is not None else None,
        )
        return self._sync.run(cursor)

    async def sync_in_background(self, on_change: Callable[[Change], Awaitable[None]]) -> None:
        """As `Connector.sync_in_background`, then commits a cursor no change carried — the
        change feed is the whole account's, so a sync may end on pages with nothing in this
        folder, and its cursor would otherwise never move past them."""
        await super().sync_in_background(on_change)
        if self._sync is not None and self._sync.trailing is not None:
            await self._commit_cursor(self._sync.trailing)

    async def _load_cursor(self) -> SyncCursor | None:
        return await self._state.load_cursor()

    async def _commit_cursor(self, cursor: SyncCursor) -> None:
        folders = self._sync.take_folder_updates() if self._sync is not None else []
        await self._state.commit_cursor(cursor, folders)

    async def get_item(self, item_id: str) -> Item | None:
        return await self._state.get_item(item_id)

    async def _save_item(self, item: Item) -> None:
        await self._state.save_item(item)

    async def _delete_item(self, item_id: str) -> None:
        await self._state.delete_item(item_id)

    async def create_webhook(self, notification_url: str) -> Subscription:
        """Open a channel on the change feed; Drive POSTs to `notification_url` (HTTPS) on
        every change, with an empty body and the channel in `X-Goog-*` headers — pass those
        headers to `verify_webhook_notification`. The channel lives at most 7 days."""
        channel_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        expiration = datetime.now(UTC) + _MAX_CHANNEL_LIFETIME
        page_token = await self.client.get_start_page_token(self._drive_id)
        raw = await self.client.watch_changes(
            page_token,
            self._drive_id,
            channel_id=channel_id,
            address=notification_url,
            token=token,
            expiration_ms=int(expiration.timestamp() * 1000),
        )
        if raw.get("expiration"):
            expiration = datetime.fromtimestamp(int(raw["expiration"]) / 1000, UTC)
        resource_id = str(raw["resourceId"])
        await self._state.save_webhook(
            channel_id, resource_id, token, notification_url, expiration.isoformat()
        )
        return Subscription(
            id=channel_id,
            resource=resource_id,
            notification_url=notification_url,
            expiration=expiration,
            client_state=token,
        )

    async def renew_webhook(self, subscription_id: str) -> Subscription:
        """Drive channels can't be extended: a new channel is opened to the same URL and the
        old one stopped, so the returned subscription has a new id."""
        state = await self._tracked_webhook(subscription_id)
        old_channel, old_resource = state.channel_id, state.resource_id
        subscription = await self.create_webhook(state.notification_url)
        await self.client.stop_channel(old_channel, old_resource)
        return subscription

    async def delete_webhook(self, subscription_id: str) -> None:
        state = await self._tracked_webhook(subscription_id)
        await self.client.stop_channel(state.channel_id, state.resource_id)
        await self._state.clear_webhook(subscription_id)

    async def list_webhooks(self) -> builtins.list[Subscription]:
        """This connector's channel, from its own store — Drive has no API listing them."""
        state = await self._state.load_webhook()
        if state is None:
            return []
        return [
            Subscription(
                id=state.channel_id,
                resource=state.resource_id,
                notification_url=state.notification_url,
                expiration=datetime.fromisoformat(state.expiration),
                client_state=state.token,
            )
        ]

    async def verify_webhook_notification(self, payload: dict[str, Any]) -> bool:
        """Whether a notification came from this connector's channel. Drive sends no body:
        `payload` is the request's headers, `X-Goog-Channel-Id` and `X-Goog-Channel-Token`
        compared with the channel's. The first notification after `create_webhook` has
        `X-Goog-Resource-State: sync` and announces the channel rather than a change."""
        state = await self._state.load_webhook()
        if state is None:
            return False
        headers = {str(key).lower(): str(value) for key, value in payload.items()}
        return headers.get("x-goog-channel-id") == state.channel_id and secrets.compare_digest(
            headers.get("x-goog-channel-token", ""), state.token
        )

    async def _tracked_webhook(self, subscription_id: str) -> GoogleDriveWebhookState:
        state = await self._state.load_webhook()
        if state is None or state.channel_id != subscription_id:
            raise NotFoundError(
                f"channel {subscription_id!r} is not this connector's — Drive needs the "
                "resourceId it was created with to stop it"
            )
        return state

    async def get_permissions(self, item: Item) -> builtins.list[Permission]:
        await self._inside(item.id)
        return [
            Permission(
                principal=str(
                    raw.get("emailAddress")
                    or raw.get("domain")
                    or ("anyone" if raw.get("type") == "anyone" else raw.get("id", ""))
                ),
                principal_type=_PRINCIPAL_TYPES[raw["type"]],
                role=_ROLES[raw["role"]],
            )
            async for raw in self.client.list_permissions(item.id)
            if raw.get("type") in _PRINCIPAL_TYPES and raw.get("role") in _ROLES
        ]

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
