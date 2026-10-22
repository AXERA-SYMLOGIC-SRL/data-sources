"""One `sync()` of a Google Drive folder: the first one walks it, every later one reads Drive's
change feed from the cursor and keeps what concerns the connected folder.

Drive's change feed covers the whole account (or the whole shared drive), not one folder, and
says nothing about where a file *used* to be. So a change is placed by its file's current
ancestry; a file is reported deleted only if the item index knows it was inside; and a folder
change is compared with where that folder was at the last committed sync (`DriveIndex`): a
folder that came in is walked (Drive reports no change for the files inside it), one that
moved or was renamed is walked again (its files' paths changed), and one that left has its
indexed files reported deleted. Without an index (a connector used without a store) only
file changes are followed and nothing is reported deleted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, Protocol

from data_sources.connectors.google_drive.client import FOLDER_MIME, GoogleDriveClient
from data_sources.connectors.google_drive.items import (
    Exports,
    is_document,
    parse_datetime,
    to_item,
)
from data_sources.connectors.google_drive.scope import Ancestry, is_excluded, join, walk
from data_sources.connectors.google_drive.state import FolderUpdate
from data_sources.core.models import Change, ChangeType, SyncCursor


class DriveIndex(Protocol):
    async def known(self, item_id: str) -> bool: ...

    async def folder_path(self, folder_id: str) -> str | None: ...

    async def items_under(self, path: str) -> list[str]: ...

    async def folders_under(self, path: str) -> list[str]: ...


class DriveSync:
    """Turns one sync's reads into `Change`s. The last change of each change-feed page
    carries the cursor after that page.

    What the connector commits besides the cursor is kept here until it's committed:
    `folder_updates` (where folders now are — applied with the cursor, since a folder moving
    only counts once the changes about its files are processed), and `trailing`, a cursor no
    change could carry because the last pages held nothing inside the folder — without it,
    a busy account with a quiet folder would re-read the same feed every sync.
    """

    def __init__(
        self,
        client: GoogleDriveClient,
        root_id: str,
        drive_id: str | None,
        excluded: Sequence[str],
        exports: Exports,
        index: DriveIndex | None,
    ) -> None:
        self._client = client
        self._root_id = root_id
        self._drive_id = drive_id
        self._excluded = excluded
        self._exports = exports
        self._index = index
        self._ancestry = Ancestry(client, root_id)
        #: Folder moves seen this sync, latest last — also consulted before the index, which
        #: only learns them at the next cursor commit.
        self._folders_now: dict[str, str | None] = {}
        self._pending: list[FolderUpdate] = []
        self.trailing: SyncCursor | None = None

    def take_folder_updates(self) -> list[FolderUpdate]:
        updates, self._pending = self._pending, []
        return updates

    async def run(self, cursor: SyncCursor | None) -> AsyncIterator[Change]:
        if cursor is None or not cursor.token:
            # Taken before the walk, so a change made while walking is read again next sync
            # rather than missed.
            token = await self._client.get_start_page_token(self._drive_id)
            async for change in self._carrying(self._walk(self._root_id, "", initial=True), token):
                yield change
            return

        async for page, resume in self._client.list_changes(cursor.token, self._drive_id):
            async for change in self._carrying(self._page(page), resume):
                yield change

    async def _carrying(self, changes: AsyncIterator[Change], token: str) -> AsyncIterator[Change]:
        """`changes`, the last one carrying `token` as its cursor; when there are none,
        `token` becomes `trailing`."""
        cursor = SyncCursor(token=token)
        previous: Change | None = None
        async for change in changes:
            if previous is not None:
                yield previous
            previous = change
        if previous is None:
            self.trailing = cursor
            return
        self.trailing = None
        yield previous.model_copy(update={"cursor": cursor})

    async def _walk(self, folder_id: str, path: str, *, initial: bool) -> AsyncIterator[Change]:
        """A change for every document under `folder_id`, recording each folder's path."""
        async for raw, parent_path in walk(
            self._client, folder_id, path, self._drive_id, self._excluded
        ):
            if raw.get("mimeType") == FOLDER_MIME:
                folder_path = join(parent_path, str(raw.get("name", "")))
                self._ancestry.remember(raw["id"], folder_path)
                self._record_folder(raw["id"], folder_path)
            elif is_document(raw, self._exports):
                yield await self._file_change(raw, parent_path, initial=initial)

    async def _page(self, page: list[dict[str, Any]]) -> AsyncIterator[Change]:
        for raw_change in page:
            if raw_change.get("changeType") == "drive":
                continue
            file_id = str(raw_change.get("fileId", ""))
            if not file_id or file_id == self._root_id:
                continue
            raw = raw_change.get("file")
            live = raw is not None and not raw_change.get("removed") and not raw.get("trashed")

            if live and raw is not None and raw.get("mimeType") == FOLDER_MIME:
                async for change in self._folder_change(file_id, raw):
                    yield change
                continue

            if live and raw is not None and is_document(raw, self._exports):
                parent_path = await self._ancestry.parent_path(raw)
                if parent_path is not None and not is_excluded(parent_path, self._excluded):
                    yield await self._file_change(raw, parent_path, initial=False)
                    continue

            async for change in self._gone(file_id, raw_change):
                yield change

    async def _file_change(self, raw: dict[str, Any], parent_path: str, *, initial: bool) -> Change:
        if self._index is not None:
            known = await self._index.known(str(raw["id"]))
            change_type = ChangeType.UPDATED if known else ChangeType.CREATED
        elif initial or raw.get("createdTime") == raw.get("modifiedTime"):
            change_type = ChangeType.CREATED
        else:
            change_type = ChangeType.UPDATED
        return Change(
            item_id=str(raw["id"]),
            type=change_type,
            item=to_item(raw, parent_path, self._exports),
            timestamp=parse_datetime(raw.get("modifiedTime")),
        )

    async def _gone(self, file_id: str, raw_change: dict[str, Any]) -> AsyncIterator[Change]:
        """A file or folder that is no longer inside the root (or never was): reported only
        when the index says it used to be."""
        if self._index is None:
            return
        if await self._index.known(file_id):
            yield Change(
                item_id=file_id,
                type=ChangeType.DELETED,
                timestamp=parse_datetime(raw_change.get("time")),
            )
            return
        old_path = await self._recorded_folder(file_id)
        if old_path is not None:
            async for change in self._folder_left(file_id, old_path):
                yield change

    async def _folder_change(self, folder_id: str, raw: dict[str, Any]) -> AsyncIterator[Change]:
        if self._index is None:
            return
        parent_path = await self._ancestry.parent_path(raw)
        new_path = None if parent_path is None else join(parent_path, str(raw.get("name", "")))
        if new_path is not None and is_excluded(new_path, self._excluded):
            new_path = None
        self._ancestry.remember(folder_id, new_path)

        old_path = await self._recorded_folder(folder_id)
        if new_path == old_path:
            return
        if new_path is None:
            assert old_path is not None
            async for change in self._folder_left(folder_id, old_path):
                yield change
            return
        self._record_folder(folder_id, new_path)
        async for change in self._walk(folder_id, new_path, initial=False):
            yield change

    async def _folder_left(self, folder_id: str, old_path: str) -> AsyncIterator[Change]:
        assert self._index is not None
        for item_id in await self._index.items_under(old_path):
            yield Change(item_id=item_id, type=ChangeType.DELETED)
        for below in await self._index.folders_under(old_path):
            self._record_folder(below, None)
        self._record_folder(folder_id, None)

    async def _recorded_folder(self, folder_id: str) -> str | None:
        if folder_id in self._folders_now:
            return self._folders_now[folder_id]
        return await self._index.folder_path(folder_id) if self._index is not None else None

    def _record_folder(self, folder_id: str, path: str | None) -> None:
        self._folders_now[folder_id] = path
        self._pending.append((folder_id, path))
