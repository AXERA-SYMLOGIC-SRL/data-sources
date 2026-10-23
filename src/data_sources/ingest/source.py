from __future__ import annotations

from datetime import date
from typing import Any, Protocol

from pydantic import BaseModel


class ReportTable(BaseModel):
    """A report as the loader stores it: column names, rows of strings, and which column (if
    any) holds each row's day."""

    headers: list[str]
    rows: list[list[str]]
    day_column: str | None = None


class TabularSource(Protocol):
    """What a connector offers the loader. `GoogleAnalyticsConnector` and `MetaAdsConnector`
    implement it; `spec` is whatever query model that connector takes."""

    #: How many of the most recent days an API may still revise after reporting them —
    #: each load fetches that far back again and replaces what it had.
    revision_days: int

    def day_column(self, spec: Any) -> str | None:
        """The column holding each row's day, or `None` when `spec` has no daily split."""

    async def read_table(self, spec: Any, since: date | None, until: date | None) -> ReportTable:
        """Run `spec` over `since`..`until` (inclusive; the spec's own period when `None`)."""


class LoadResult(BaseModel):
    """What one load did: rows written and the period it replaced (`None` for a snapshot)."""

    rows: int
    since: date | None = None
    until: date | None = None
