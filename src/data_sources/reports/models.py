from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass
class RawReport:
    """A report exactly as the provider returned it.

    `records` are the API's own rows (GA's `{"dimensionValues", "metricValues"}`, Meta's
    insight objects, ...); `days` is the day of each, parallel to it (None when the report has
    no daily split). `envelope` is what describes the rows: the rest of GA's first response
    (headers, metadata, quota), or the account the report was read for.
    """

    records: list[dict[str, Any]]
    days: list[date | None]
    envelope: dict[str, Any] = field(default_factory=dict)


def parse_day(value: str) -> date | None:
    """A report's day cell: `20261022` (Google Analytics) or `2026-10-22`."""
    try:
        return datetime.strptime(value, "%Y%m%d" if len(value) == 8 else "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
