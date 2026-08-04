from __future__ import annotations

import json
import mimetypes
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

from data_sources.config.schema import ConnectorConfig
from data_sources.connectors.sftp.client import SFTPClient
from data_sources.connectors.sftp.entries import RemoteClient, RemoteEntry
from data_sources.connectors.sftp.ftp_client import FTPClient
from data_sources.connectors.sftp.models import SFTPItemRecord, SFTPSyncState
from data_sources.core.connector import Connector
from data_sources.core.exceptions import ConfigurationError, ConnectionError, DataSourceError
from data_sources.core.models import Change, ChangeType, Item, ItemType, SyncCursor
from data_sources.core.registry import register_connector

#: Scheme -> default port, and the set of schemes `options.url` may use.
_DEFAULT_PORTS = {"sftp": 22, "ftp": 21}


@register_connector("sftp")
class SFTPConnector(Connector):
    """Connector for a single SFTP or FTP server, addressed via `config.options["url"]`
    (e.g. `sftp://host:22/reports` or `ftp://host:21/reports`) and rooted at that URL's
    path. The scheme picks the wire protocol — `SFTPClient` (asyncssh) for `sftp://`,
    `FTPClient` (aioftp, plain FTP only — no FTPS) for `ftp://` — but every other
    behavior (listing, sync-diffing, download) is identical between the two; both
    clients implement the same `RemoteClient` protocol.

    Neither protocol exposes a delta/change feed or a webhook mechanism, so `sync()`
    instead diffs a full recursive scan of the tree against the previous scan's
    snapshot (see `SFTPSyncState`).

    Only regular files are surfaced by `list(recursive=True)` and `sync()` — matching
    `RemoteClient.walk`, which skips directories (and, for SFTP, symlinks and other
    special files). `list(recursive=False)` browses one directory level and includes
    subfolders.

    Every item `sync_in_background` sees is indexed locally, so a caller that only kept a
    `Change.item_id` (here, the file's absolute path) can later call `get_item(item_id)`
    to recover the `Item` (and then `download(item)`) without a live call to the server.
    """

    provider = "sftp"
    supports_sync = True
    supports_item_lookup = True
    models = (SFTPSyncState, SFTPItemRecord)

    def __init__(self, config: ConnectorConfig) -> None:
        super().__init__(config)

        url = config.options.get("url")
        if not url:
            raise ConfigurationError("sftp connector requires options.url")

        parsed = urlparse(url)
        if parsed.scheme not in _DEFAULT_PORTS:
            raise ConfigurationError(
                "sftp connector options.url must use sftp:// or ftp://, got "
                f"{parsed.scheme or '(none)'!r}"
            )
        if not parsed.hostname:
            raise ConfigurationError("sftp connector options.url is missing a host")

        self._url = url
        self._scheme = parsed.scheme
        self._host = parsed.hostname
        self._port = parsed.port or _DEFAULT_PORTS[self._scheme]
        self._root_path = parsed.path or "/"
        self._excluded_paths = tuple(config.options.get("excluded_paths", ()))
        self._client: RemoteClient | None = None

    @property
    def client(self) -> RemoteClient:
        if self._client is None:
            raise ConnectionError(
                f"{self.provider} connector is not connected; call connect() first"
            )
        return self._client

    @property
    def _sync_key(self) -> str:
        return self.config.name or self._url

    async def connect(self) -> None:
        creds = (self.config.auth and self.config.auth.credentials) or {}
        username = creds.get("username")
        if not username:
            raise ConfigurationError("sftp connector auth.credentials missing: username")

        if self._scheme == "sftp":
            if not creds.get("password") and not creds.get("private_key"):
                raise ConfigurationError(
                    "sftp connector auth.credentials requires 'password' or 'private_key'"
                )
            self._client = await SFTPClient.connect(
                self._host,
                self._port,
                username,
                password=creds.get("password"),
                private_key=creds.get("private_key"),
                passphrase=creds.get("passphrase"),
                known_hosts=creds.get("known_hosts"),
            )
        else:
            self._client = await FTPClient.connect(
                self._host, self._port, username, password=creds.get("password")
            )

    async def validate(self) -> bool:
        try:
            await self.client.stat(self._root_path)
        except DataSourceError:
            return False
        return True

    async def list(
        self, path: str | None = None, *, recursive: bool = False
    ) -> AsyncIterator[Item]:
        item_path = path if path is not None else self._root_path

        if recursive:
            async for entry in self.client.walk(item_path):
                if self._is_excluded(entry):
                    continue
                yield self._to_item(entry)
        else:
            async for entry in self.client.listdir(item_path):
                if self._is_excluded(entry):
                    continue
                yield self._to_item(entry)

    async def get_metadata(self, item_id: str) -> Item:
        entry = await self.client.stat(item_id)
        return self._to_item(entry)

    async def download(self, item: Item) -> AsyncIterator[bytes]:
        async for chunk in self.client.download(item.id):
            yield chunk

    async def sync(self, cursor: SyncCursor | None = None) -> AsyncIterator[Change]:
        previous_snapshot: dict[str, str] = (
            json.loads(cursor.token) if cursor and cursor.token else {}
        )

        current_snapshot: dict[str, str] = {}
        entries: dict[str, RemoteEntry] = {}
        async for entry in self.client.walk(self._root_path):
            if self._is_excluded(entry):
                continue
            entries[entry.path] = entry
            current_snapshot[entry.path] = entry.mtime.isoformat() if entry.mtime else ""

        created_or_updated = []
        for path, mtime in current_snapshot.items():
            previous_mtime = previous_snapshot.get(path)
            if previous_mtime is None:
                created_or_updated.append((path, ChangeType.CREATED))
            elif previous_mtime != mtime:
                created_or_updated.append((path, ChangeType.UPDATED))

        deleted = [path for path in previous_snapshot if path not in current_snapshot]

        new_cursor = SyncCursor(token=json.dumps(current_snapshot))
        total = len(created_or_updated) + len(deleted)
        emitted = 0

        for path, change_type in created_or_updated:
            emitted += 1
            entry = entries[path]
            yield Change(
                item_id=path,
                type=change_type,
                item=self._to_item(entry),
                timestamp=entry.mtime,
                cursor=new_cursor if emitted == total else None,
            )

        for path in deleted:
            emitted += 1
            yield Change(
                item_id=path,
                type=ChangeType.DELETED,
                cursor=new_cursor if emitted == total else None,
            )

    async def _load_cursor(self) -> SyncCursor | None:
        assert self.store is not None
        async with self.store.session() as session:
            state = await session.get(SFTPSyncState, self._sync_key)
            return SyncCursor(token=state.snapshot) if state else None

    async def _commit_cursor(self, cursor: SyncCursor) -> None:
        assert self.store is not None
        async with self.store.session() as session:
            state = await session.get(SFTPSyncState, self._sync_key)
            if state is None:
                session.add(SFTPSyncState(sync_key=self._sync_key, snapshot=cursor.token or ""))
            else:
                state.snapshot = cursor.token or ""
            await session.commit()

    async def get_item(self, item_id: str) -> Item | None:
        assert self.store is not None
        async with self.store.session() as session:
            record = await session.get(SFTPItemRecord, (self._sync_key, item_id))
            return Item.model_validate_json(record.data) if record else None

    async def _save_item(self, item: Item) -> None:
        assert self.store is not None
        async with self.store.session() as session:
            record = await session.get(SFTPItemRecord, (self._sync_key, item.id))
            if record is None:
                session.add(
                    SFTPItemRecord(
                        sync_key=self._sync_key, item_id=item.id, data=item.model_dump_json()
                    )
                )
            else:
                record.data = item.model_dump_json()
            await session.commit()

    async def _delete_item(self, item_id: str) -> None:
        assert self.store is not None
        async with self.store.session() as session:
            record = await session.get(SFTPItemRecord, (self._sync_key, item_id))
            if record is not None:
                await session.delete(record)
                await session.commit()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    def _is_excluded(self, entry: RemoteEntry) -> bool:
        if not self._excluded_paths:
            return False
        root = self._root_path.rstrip("/")
        if not entry.path.startswith(f"{root}/"):
            return False
        relative = entry.path[len(root) + 1 :]
        return any(relative.startswith(excluded) for excluded in self._excluded_paths)

    def _to_item(self, entry: RemoteEntry) -> Item:
        return Item(
            id=entry.path,
            name=entry.name,
            type=ItemType.FOLDER if entry.is_dir else ItemType.FILE,
            path=entry.path.rsplit("/", 1)[0] or "/",
            parent_id=entry.path.rsplit("/", 1)[0] or "/",
            size=entry.size,
            mime_type=None if entry.is_dir else mimetypes.guess_type(entry.name)[0],
            created_at=None,
            modified_at=entry.mtime,
            metadata=_metadata(entry),
        )


def _metadata(entry: RemoteEntry) -> dict[str, Any]:
    return {"permissions": oct(entry.permissions)} if entry.permissions is not None else {}
