from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class RemoteEntry:
    """A single file or directory as reported by an SFTP or FTP server.

    `path` is always absolute (rooted at `/`), which is what `SFTPConnector` uses as an
    `Item.id` — neither protocol has an id distinct from location, unlike Graph's
    driveItem ids. `permissions` is the raw POSIX mode bits when the server exposed
    them (always true for SFTP; not all FTP servers report them) and `None` otherwise.
    """

    path: str
    name: str
    is_dir: bool
    size: int | None
    mtime: datetime | None
    permissions: int | None


class RemoteClient(Protocol):
    """The subset of `SFTPClient`/`FTPClient` `SFTPConnector` depends on.

    `SFTPConnector` is written against this protocol rather than either concrete
    client so the same connector logic (listing, sync-diffing, download) works
    unchanged regardless of which protocol `options.url` selects.
    """

    async def stat(self, path: str) -> RemoteEntry: ...

    def listdir(self, path: str) -> AsyncIterator[RemoteEntry]: ...

    def walk(self, path: str) -> AsyncIterator[RemoteEntry]: ...

    def download(self, path: str) -> AsyncIterator[bytes]: ...

    async def close(self) -> None: ...
