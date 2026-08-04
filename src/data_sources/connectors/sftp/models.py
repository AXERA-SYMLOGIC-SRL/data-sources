from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from data_sources.store.models import Base


class SFTPSyncState(Base):
    """Persists an `SFTPConnector` instance's own directory-tree snapshot between runs.

    SFTP exposes no delta/change feed, so `sync()` diffs a full recursive scan against
    the previous scan's snapshot (`path -> mtime`, JSON-encoded) rather than resuming
    from a server-provided cursor.
    """

    __tablename__ = "sftp_sync_state"

    sync_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    snapshot: Mapped[str] = mapped_column(Text)


class SFTPItemRecord(Base):
    """Backs `SFTPConnector.get_item` — the last-synced `Item`, as JSON, per path.

    Keyed by `(sync_key, item_id)` rather than `item_id` alone since one store may back
    several `SFTPConnector`s (different hosts, or different root paths on the same host).
    """

    __tablename__ = "sftp_items"

    sync_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(1024), primary_key=True)
    data: Mapped[str] = mapped_column(Text)
