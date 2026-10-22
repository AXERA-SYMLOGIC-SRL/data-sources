"""Everything a `GoogleDriveConnector` keeps in its `Store`, under its own sync key: the sync
cursor, the item index, where each folder was, and the active notification channel."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.orm import InstrumentedAttribute

from data_sources.connectors.google_drive.models import (
    GoogleDriveFolderRecord,
    GoogleDriveItemRecord,
    GoogleDriveSyncState,
    GoogleDriveWebhookState,
)
from data_sources.core.models import Item, SyncCursor
from data_sources.store.base import Store

#: A folder's new path relative to the root, or `None` once it's no longer inside it.
FolderUpdate = tuple[str, str | None]


class DriveState:
    def __init__(self, store: Store, sync_key: str) -> None:
        self._store = store
        self._key = sync_key

    async def load_cursor(self) -> SyncCursor | None:
        async with self._store.session() as session:
            state = await session.get(GoogleDriveSyncState, self._key)
            return SyncCursor(token=state.page_token) if state else None

    async def commit_cursor(self, cursor: SyncCursor, folders: Sequence[FolderUpdate]) -> None:
        """Move the cursor and record where folders now are, in one transaction — folder
        moves seen by sync only count once the changes before the cursor are processed."""
        async with self._store.session() as session:
            for folder_id, path in folders:
                record = await session.get(GoogleDriveFolderRecord, (self._key, folder_id))
                if path is None:
                    if record is not None:
                        await session.delete(record)
                elif record is None:
                    session.add(
                        GoogleDriveFolderRecord(sync_key=self._key, folder_id=folder_id, path=path)
                    )
                else:
                    record.path = path

            state = await session.get(GoogleDriveSyncState, self._key)
            if state is None:
                session.add(GoogleDriveSyncState(sync_key=self._key, page_token=cursor.token or ""))
            else:
                state.page_token = cursor.token or ""
            await session.commit()

    async def get_item(self, item_id: str) -> Item | None:
        async with self._store.session() as session:
            record = await session.get(GoogleDriveItemRecord, (self._key, item_id))
            return Item.model_validate_json(record.data) if record else None

    async def save_item(self, item: Item) -> None:
        async with self._store.session() as session:
            record = await session.get(GoogleDriveItemRecord, (self._key, item.id))
            if record is None:
                session.add(
                    GoogleDriveItemRecord(
                        sync_key=self._key,
                        item_id=item.id,
                        path=item.path or "",
                        data=item.model_dump_json(),
                    )
                )
            else:
                record.path = item.path or ""
                record.data = item.model_dump_json()
            await session.commit()

    async def delete_item(self, item_id: str) -> None:
        async with self._store.session() as session:
            record = await session.get(GoogleDriveItemRecord, (self._key, item_id))
            if record is not None:
                await session.delete(record)
                await session.commit()

    async def known(self, item_id: str) -> bool:
        async with self._store.session() as session:
            return await session.get(GoogleDriveItemRecord, (self._key, item_id)) is not None

    async def folder_path(self, folder_id: str) -> str | None:
        async with self._store.session() as session:
            record = await session.get(GoogleDriveFolderRecord, (self._key, folder_id))
            return record.path if record else None

    async def items_under(self, path: str) -> list[str]:
        """The ids of indexed files in the folder at `path` or anywhere below it."""
        async with self._store.session() as session:
            result = await session.execute(
                select(GoogleDriveItemRecord.item_id).where(
                    GoogleDriveItemRecord.sync_key == self._key,
                    _at_or_under(GoogleDriveItemRecord.path, path),
                )
            )
            return list(result.scalars())

    async def folders_under(self, path: str) -> list[str]:
        """The ids of recorded folders strictly below the folder at `path`."""
        async with self._store.session() as session:
            result = await session.execute(
                select(GoogleDriveFolderRecord.folder_id).where(
                    GoogleDriveFolderRecord.sync_key == self._key,
                    GoogleDriveFolderRecord.path.startswith(f"{path}/", autoescape=True),
                )
            )
            return list(result.scalars())

    async def load_webhook(self) -> GoogleDriveWebhookState | None:
        async with self._store.session() as session:
            return await session.get(GoogleDriveWebhookState, self._key)

    async def save_webhook(
        self,
        channel_id: str,
        resource_id: str,
        token: str,
        notification_url: str,
        expiration: str,
    ) -> None:
        async with self._store.session() as session:
            state = await session.get(GoogleDriveWebhookState, self._key)
            if state is None:
                state = GoogleDriveWebhookState(sync_key=self._key)
                session.add(state)
            state.channel_id = channel_id
            state.resource_id = resource_id
            state.token = token
            state.notification_url = notification_url
            state.expiration = expiration
            await session.commit()

    async def clear_webhook(self, channel_id: str) -> None:
        async with self._store.session() as session:
            state = await session.get(GoogleDriveWebhookState, self._key)
            if state is not None and state.channel_id == channel_id:
                await session.delete(state)
                await session.commit()


def _at_or_under(column: InstrumentedAttribute[str], path: str) -> ColumnElement[bool]:
    return or_(column == path, column.startswith(f"{path}/", autoescape=True))
