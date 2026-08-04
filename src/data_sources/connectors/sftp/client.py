from __future__ import annotations

import stat as stat_module
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast

import asyncssh

from data_sources.connectors.sftp.entries import RemoteEntry
from data_sources.core.exceptions import AuthenticationError, DataSourceError, NotFoundError
from data_sources.core.logging import logger

#: Read this many bytes per `download()` chunk.
_CHUNK_SIZE = 64 * 1024


class SFTPClient:
    """Thin async wrapper around asyncssh's SFTP client: authentication, directory
    traversal and content download only.

    Mapping `RemoteEntry`s onto this SDK's `Item`/`Change` domain types, tracking sync
    snapshots, and filtering by path are the connector's job, not the client's.
    """

    def __init__(self, connection: asyncssh.SSHClientConnection, sftp: asyncssh.SFTPClient) -> None:
        self._connection = connection
        self._sftp = sftp
        self.logger = logger.getChild("sftp.client")

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        username: str,
        *,
        password: str | None = None,
        private_key: str | None = None,
        passphrase: str | None = None,
        known_hosts: str | None = None,
    ) -> SFTPClient:
        client_keys = (
            [asyncssh.import_private_key(private_key, passphrase=passphrase)]
            if private_key
            else None
        )
        try:
            connection = await asyncssh.connect(
                host,
                port=port,
                username=username,
                password=password,
                client_keys=client_keys,
                known_hosts=known_hosts,
            )
        except asyncssh.PermissionDenied as exc:
            raise AuthenticationError(f"SFTP server rejected credentials: {exc}") from exc
        except asyncssh.Error as exc:
            raise DataSourceError(f"SFTP connection to {host}:{port} failed: {exc}") from exc

        sftp = await connection.start_sftp_client()
        return cls(connection, sftp)

    async def close(self) -> None:
        self._sftp.exit()
        await self._sftp.wait_closed()
        self._connection.close()
        await self._connection.wait_closed()

    async def stat(self, path: str) -> RemoteEntry:
        try:
            attrs = await self._sftp.stat(path)
        except asyncssh.SFTPNoSuchFile as exc:
            raise NotFoundError(f"SFTP path not found: {path}") from exc
        except asyncssh.SFTPError as exc:
            raise DataSourceError(f"SFTP error for {path}: {exc}") from exc
        return _to_entry(path, attrs)

    async def listdir(self, path: str) -> AsyncIterator[RemoteEntry]:
        try:
            names = await self._sftp.readdir(path)
        except asyncssh.SFTPNoSuchFile as exc:
            raise NotFoundError(f"SFTP path not found: {path}") from exc
        except asyncssh.SFTPError as exc:
            raise DataSourceError(f"SFTP error for {path}: {exc}") from exc

        for entry in names:
            filename = (
                entry.filename.decode("utf-8")
                if isinstance(entry.filename, bytes)
                else entry.filename
            )
            if filename in (".", ".."):
                continue
            entry_path = f"{path.rstrip('/')}/{filename}"
            yield _to_entry(entry_path, entry.attrs)

    async def walk(self, path: str) -> AsyncIterator[RemoteEntry]:
        """Recursively yield every file under `path` (directories are not yielded).

        Symlinks and other non-regular, non-directory entries are skipped — SFTP's
        `readdir` reports them via `lstat`-style attributes, and following them risks
        walking into a cycle.
        """
        async for entry in self.listdir(path):
            if entry.is_dir:
                async for nested in self.walk(entry.path):
                    yield nested
            elif entry.permissions is not None and stat_module.S_ISREG(entry.permissions):
                yield entry

    async def download(self, path: str) -> AsyncIterator[bytes]:
        try:
            async with self._sftp.open(path, "rb") as f:
                while True:
                    chunk = await f.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    yield cast(bytes, chunk)
        except asyncssh.SFTPNoSuchFile as exc:
            raise NotFoundError(f"SFTP path not found: {path}") from exc
        except asyncssh.SFTPError as exc:
            raise DataSourceError(f"SFTP error downloading {path}: {exc}") from exc


def _to_entry(path: str, attrs: asyncssh.SFTPAttrs) -> RemoteEntry:
    permissions = attrs.permissions
    is_dir = permissions is not None and stat_module.S_ISDIR(permissions)
    mtime = datetime.fromtimestamp(attrs.mtime, tz=UTC) if attrs.mtime is not None else None
    return RemoteEntry(
        path=path,
        name=path.rsplit("/", 1)[-1],
        is_dir=is_dir,
        size=attrs.size,
        mtime=mtime,
        permissions=permissions,
    )
