from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from data_sources.store.models import Base


class ReportRow(Base):
    """One row of a loaded report. `connector` is the caller's name for the connection (one
    per ad account or property), `report` the name of the query, `day` the date the row is
    for — `NULL` for a report with no daily split. `data` is the row as `{column: value}`,
    every value the API's own string (nested values as JSON text); JSONB on PostgreSQL, so
    `data->>'sessions'` and casts work in SQL.
    """

    __tablename__ = "report_rows"
    __table_args__ = (Index("ix_report_rows_report_day", "connector", "report", "day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    connector: Mapped[str] = mapped_column(String(255))
    report: Mapped[str] = mapped_column(String(255))
    day: Mapped[date | None] = mapped_column(Date, nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
