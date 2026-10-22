from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from data_sources.store.models import Base


class GoogleDriveSyncState(Base):
    """Persists a `GoogleDriveConnector` instance's own change-feed page token between runs."""

    __tablename__ = "google_drive_sync_state"

    sync_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    page_token: Mapped[str] = mapped_column(Text)


class GoogleDriveItemRecord(Base):
    """Backs `GoogleDriveConnector.get_item` — the last-synced `Item`, as JSON, per id — and
    tells sync which files were inside the connected folder: Drive doesn't say where a
    removed or moved-out file used to be, so only one recorded here is reported deleted.

    `path` (the item's parent folder, relative to the root) is kept beside the JSON so the
    files under a folder that left the root can be found without reading every record.
    """

    __tablename__ = "google_drive_items"

    sync_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    path: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[str] = mapped_column(Text)


class GoogleDriveFolderRecord(Base):
    """Where each folder inside the root was at the last committed sync — so a folder change
    can be told apart as one that came in (walk it), moved or was renamed (walk it again, its
    files' paths changed) or left (its files are deleted), from one that didn't move.
    """

    __tablename__ = "google_drive_folders"

    sync_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    folder_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    path: Mapped[str] = mapped_column(Text)


class GoogleDriveWebhookState(Base):
    """The connector's active notification channel: its id and Drive's `resourceId` (both
    needed to stop it — Drive has no API listing channels), and the secret `token` Drive
    echoes in `X-Goog-Channel-Token`, so a notification can be verified as this channel's."""

    __tablename__ = "google_drive_webhook_state"

    sync_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(255))
    resource_id: Mapped[str] = mapped_column(String(255))
    token: Mapped[str] = mapped_column(String(255))
    notification_url: Mapped[str] = mapped_column(Text)
    #: ISO 8601, UTC — kept as text so SQLite and PostgreSQL round-trip it alike.
    expiration: Mapped[str] = mapped_column(String(64))
