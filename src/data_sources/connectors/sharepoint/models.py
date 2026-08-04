from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from data_sources.store.models import Base


class SharePointSyncState(Base):
    """Persists a `SharePointConnector` instance's own delta-link cursor between runs."""

    __tablename__ = "sharepoint_sync_state"

    drive_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    delta_link: Mapped[str] = mapped_column(Text)


class SharePointItemRecord(Base):
    """Backs `SharePointConnector.get_item` — the last-synced `Item`, as JSON, per id.

    Keyed by `(drive_key, item_id)` rather than `item_id` alone since Graph item ids are
    only unique within a single drive, and one store may back several drives' connectors.
    """

    __tablename__ = "sharepoint_items"

    drive_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    data: Mapped[str] = mapped_column(Text)
