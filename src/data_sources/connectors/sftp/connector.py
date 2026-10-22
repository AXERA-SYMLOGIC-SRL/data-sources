from __future__ import annotations

import contextlib
import json
import mimetypes
import posixpath
import stat as stat_module
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

from data_sources.config.schema import ConnectorConfig
from data_sources.connectors.sftp.entries import RemoteClient, RemoteEntry
from data_sources.connectors.sftp.ftp_client import FTPClient
from data_sources.connectors.sftp.models import SFTPItemRecord, SFTPSyncState
from data_sources.connectors.sftp.sftp_client import SFTPClient
from data_sources.core.connector import Connector
from data_sources.core.exceptions import (
    ConfigurationError,
    ConflictError,
    ConnectionError,
    DataSourceError,
    NotFoundError,
    UnsupportedOperationError,
)
from data_sources.core.logging import logger
from data_sources.core.models import (
    Change,
    ChangeType,
    ConflictBehavior,
    Item,
    ItemType,
    SyncCursor,
)
from data_sources.core.paths import check_name, is_excluded, relative_path, renamed
from data_sources.core.registry import register_connector

#: How many `name (n)` candidates a rename tries before giving up.
_MAX_RENAMES = 1000

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

    The URL's path is a boundary: `list(path)` takes `path` relative to it (or absolute
    and inside it), and `get_metadata`/`download` refuse a path outside it, an excluded
    one, a symlink, or one that only reaches it through a linked folder (`NotFoundError`).

    Every item `sync_in_background` sees is indexed locally, so a caller that only kept a
    `Change.item_id` (here, the file's absolute path) can later call `get_item(item_id)`
    to recover the `Item` (and then `download(item)`) without a live call to the server.
    """

    provider = "sftp"
    supports_sync = True
    supports_item_lookup = True
    supports_write = True
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
        self._real_root: str | None = None
        self.logger = logger.getChild("sftp.connector")

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
        except DataSourceError as exc:
            self.logger.warning(
                f"validate() failed for {self._url} (root path {self._root_path!r}): {exc}"
            )
            return False
        return True

    async def list(
        self, path: str | None = None, *, recursive: bool = False
    ) -> AsyncIterator[Item]:
        item_path = await self._inside(self._resolve(path))
        if self._is_excluded_path(item_path):
            return

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
        """`item_id`'s (an absolute path's) metadata, refused (`NotFoundError`) unless it is
        a non-excluded file or folder under the root — symlinks included: a link is refused,
        and so is a path whose resolved location (through any linked folder) leaves it."""
        path = await self._inside(self._resolve(item_id))
        if self._is_excluded_path(path):
            raise NotFoundError(f"{item_id!r} is not inside the connected folder")
        entry = await self.client.lstat(path)
        if entry.permissions is not None and stat_module.S_ISLNK(entry.permissions):
            raise NotFoundError(f"{item_id!r} is a symlink")
        return self._to_item(entry)

    async def download(self, item: Item) -> AsyncIterator[bytes]:
        # Checked again by id, whatever `item` says: the caller may have built it.
        checked = await self.get_metadata(item.id)
        async for chunk in self.client.download(checked.id):
            yield chunk

    async def upload(
        self,
        folder: str | None,
        name: str,
        content: bytes,
        *,
        mime_type: str | None = None,
        on_conflict: ConflictBehavior = ConflictBehavior.FAIL,
    ) -> Item:
        """See `Connector.upload`. The content is written to a hidden temporary name beside
        the target, then renamed onto it, so nobody reads a half-written file. Checking the
        name is taken and writing aren't one step: a file appearing in between can still be
        written over (SFTP) or refused by the server."""
        target_dir = await self._ensure_dir(self._resolve(folder))
        target, overwrite = await self._target(target_dir, check_name(name), on_conflict)
        temporary = posixpath.join(target_dir, f".{name}.{uuid.uuid4().hex}.part")
        await self.client.write(temporary, content)
        try:
            await self.client.rename(temporary, target, overwrite=overwrite)
        except BaseException:
            with contextlib.suppress(DataSourceError):
                await self.client.remove(temporary)
            raise
        return self._to_item(await self.client.lstat(target))

    async def create_folder(self, path: str) -> Item:
        return self._to_item(await self.client.lstat(await self._ensure_dir(self._resolve(path))))

    async def delete(self, item_id: str, *, permanent: bool = False) -> None:
        """See `Connector.delete`. Neither protocol has a bin: always permanent. A folder
        must be empty."""
        path = await self._inside(self._resolve(item_id))
        if path == posixpath.normpath(self._root_path):
            raise UnsupportedOperationError("the connected folder itself can't be deleted")
        entry = await self.client.lstat(path)
        if entry.permissions is not None and stat_module.S_ISLNK(entry.permissions):
            raise NotFoundError(f"{item_id!r} is a symlink")
        if entry.is_dir:
            await self.client.rmdir(path)
        else:
            await self.client.remove(path)

    async def _ensure_dir(self, target: str) -> str:
        """The absolute `target` (under the root) as an existing directory, each missing
        level made one at a time — and refused when a level is a symlink or a file, so
        nothing is ever created through a link to outside."""
        root = posixpath.normpath(self._root_path)
        current = root
        relative = posixpath.relpath(target, root)
        for part in [] if relative == "." else relative.split("/"):
            current = posixpath.join(current, part)
            entry = await self._existing(current)
            if entry is None:
                await self.client.mkdir(current)
            elif entry.permissions is not None and stat_module.S_ISLNK(entry.permissions):
                raise NotFoundError(f"{current!r} is a symlink")
            elif not entry.is_dir:
                raise ConflictError(f"{current!r} is a file, not a folder")
        return target

    async def _target(
        self, folder: str, name: str, on_conflict: ConflictBehavior
    ) -> tuple[str, bool]:
        """Where to write `name` in `folder`, and whether over a file already there."""
        target = posixpath.join(folder, name)
        existing = await self._existing(target)
        if existing is None:
            return target, False
        if on_conflict is ConflictBehavior.REPLACE:
            is_link = existing.permissions is not None and stat_module.S_ISLNK(existing.permissions)
            if existing.is_dir or is_link:
                raise ConflictError(f"{target!r} is a folder or a symlink; can't write over it")
            return target, True
        if on_conflict is ConflictBehavior.RENAME:
            for n in range(1, _MAX_RENAMES + 1):
                candidate = posixpath.join(folder, renamed(name, n))
                if await self._existing(candidate) is None:
                    return candidate, False
        raise ConflictError(f"{target!r} already exists")

    async def _existing(self, path: str) -> RemoteEntry | None:
        try:
            return await self.client.lstat(path)
        except NotFoundError:
            return None

    def _resolve(self, path: str | None) -> str:
        """A caller's path as an absolute one under the root — relative to the root, or
        absolute and inside it — normalized; `NotFoundError` when it leads out."""
        root = posixpath.normpath(self._root_path)
        if path is None or not path.strip():
            return root
        if path.startswith("/"):
            resolved = posixpath.normpath(path)
        else:
            relative = relative_path(path)
            resolved = posixpath.join(root, relative) if relative else root
        if not _under(resolved, root):
            raise NotFoundError(f"path {path!r} must stay inside the connected folder")
        return resolved

    async def _inside(self, path: str) -> str:
        """`path`, once the server confirms its resolved location (symlinks followed) is
        still under the root's own resolved location."""
        if self._real_root is None:
            self._real_root = await self.client.realpath(self._root_path)
        if not _under(await self.client.realpath(path), self._real_root):
            raise NotFoundError(f"path {path!r} leads outside the connected folder")
        return path

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
        return self._is_excluded_path(entry.path)

    def _is_excluded_path(self, path: str) -> bool:
        """Whether the absolute `path` is, or sits inside, an excluded folder — matched by
        whole segments relative to the root."""
        root = posixpath.normpath(self._root_path)
        if not self._excluded_paths or not _under(path, root) or path == root:
            return False
        return is_excluded(posixpath.relpath(path, root), self._excluded_paths)

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


def _under(path: str, root: str) -> bool:
    return root == "/" or path == root or path.startswith(f"{root.rstrip('/')}/")


def _metadata(entry: RemoteEntry) -> dict[str, Any]:
    return {"permissions": oct(entry.permissions)} if entry.permissions is not None else {}
