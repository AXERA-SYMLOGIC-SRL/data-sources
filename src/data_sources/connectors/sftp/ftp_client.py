from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import Any

import aioftp

from data_sources.connectors.sftp.entries import RemoteEntry
from data_sources.core.exceptions import AuthenticationError, DataSourceError, NotFoundError
from data_sources.core.logging import logger

#: Read this many bytes per `download()` chunk.
_CHUNK_SIZE = 64 * 1024

#: Module-level so `connect()` (a classmethod, run before any instance exists) can log
#: too; `__init__` reuses this same child logger for the instance.
_logger = logger.getChild("sftp.ftp_client")


class FTPClient:
    """Thin async wrapper around aioftp's client: authentication, directory traversal
    and content download only — matching `SFTPClient`'s surface (see `RemoteClient`)
    so `SFTPConnector` can use either interchangeably.

    Plain FTP only; FTPS (explicit or implicit TLS) isn't implemented.
    """

    def __init__(self, client: aioftp.Client) -> None:
        self._client = client
        self.logger = _logger

    @classmethod
    async def connect(
        cls, host: str, port: int, username: str, *, password: str | None = None
    ) -> FTPClient:
        client = aioftp.Client()
        try:
            await client.connect(host, port)
            await client.login(username, password or "")
        except aioftp.StatusCodeError as exc:
            client.close()
            _logger.error(f"FTP server {host}:{port} rejected credentials for {username}: {exc}")
            raise AuthenticationError(f"FTP server rejected credentials: {exc}") from exc
        except OSError as exc:
            _logger.error(f"FTP connection to {host}:{port} failed: {exc}")
            raise DataSourceError(f"FTP connection to {host}:{port} failed: {exc}") from exc
        _logger.info(f"Connected to FTP server {host}:{port} as {username}")
        return cls(client)

    async def close(self) -> None:
        self._client.close()
        self.logger.info("FTP connection closed")

    async def stat(self, path: str) -> RemoteEntry:
        try:
            info = await self._client.stat(path)
        except aioftp.StatusCodeError as exc:
            self.logger.warning(f"FTP path not found: {path}")
            raise NotFoundError(f"FTP path not found: {path}") from exc
        return _to_entry(path, info)

    async def listdir(self, path: str) -> AsyncIterator[RemoteEntry]:
        try:
            async for entry_path, info in self._client.list(path):
                yield _to_entry(str(entry_path), info)
        except aioftp.StatusCodeError as exc:
            self.logger.warning(f"FTP path not found: {path}")
            raise NotFoundError(f"FTP path not found: {path}") from exc

    async def walk(self, path: str) -> AsyncIterator[RemoteEntry]:
        """Recursively yield every file under `path` (directories are not yielded).

        Unlike `SFTPClient.walk`, this doesn't recurse manually — aioftp's `list`
        already walks the tree server-side when `recursive=True`.
        """
        try:
            async for entry_path, info in self._client.list(path, recursive=True):
                if info.get("type") == "file":
                    yield _to_entry(str(entry_path), info)
        except aioftp.StatusCodeError as exc:
            self.logger.warning(f"FTP path not found: {path}")
            raise NotFoundError(f"FTP path not found: {path}") from exc

    async def download(self, path: str) -> AsyncIterator[bytes]:
        try:
            async with self._client.download_stream(path) as stream:
                while True:
                    chunk = await stream.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
        except aioftp.StatusCodeError as exc:
            self.logger.warning(f"FTP path not found: {path}")
            raise NotFoundError(f"FTP path not found: {path}") from exc


def _to_entry(path: str, info: Mapping[str, Any]) -> RemoteEntry:
    is_dir = info.get("type") == "dir"
    size = None if is_dir else _parse_size(info.get("size"))
    return RemoteEntry(
        path=path,
        name=path.rsplit("/", 1)[-1] or path,
        is_dir=is_dir,
        size=size,
        mtime=_parse_mtime(info.get("modify")),
        #: FTP's MLSD/LIST facts don't reliably expose POSIX mode bits across servers.
        permissions=None,
    )


def _parse_size(value: str | None) -> int | None:
    return int(value) if value is not None else None


def _parse_mtime(value: str | None) -> datetime | None:
    """Parse an MLSD `modify` fact (`YYYYMMDDHHMMSS[.sss]`, always UTC) to a datetime."""
    if not value:
        return None
    return datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
