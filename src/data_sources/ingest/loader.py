from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, insert, select

from data_sources.core.logging import logger
from data_sources.ingest.models import ReportRow
from data_sources.ingest.source import LoadResult, ReportTable, TabularSource
from data_sources.store.base import Store

#: A load fetches at most this many days per request, so a long backfill is many small
#: queries (which the APIs finish) rather than one that times out.
CHUNK_DAYS = 31

_logger = logger.getChild("ingest")


def parse_day(value: str) -> date:
    """A report's day cell: `2026-10-22` (Meta) or `20261022` (Google Analytics)."""
    try:
        return datetime.strptime(value, "%Y%m%d" if len(value) == 8 else "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{value!r} is not a day in a report's day column") from exc


class ReportLoader:
    """Loads a connector's report into the `report_rows` table, and keeps it current.

    The first load of a report with a daily split backfills `backfill_days` of history up to
    yesterday (today is still filling in); every later load starts `revision_days` before
    the last day held and replaces that stretch, since the APIs revise recent days. A report
    with no daily split is replaced whole. Each period replaced is one transaction, so a
    failure part-way leaves the table as it was for that period, and loading again picks up
    from the last period that finished.

    `connector` names the connection the rows belong to (it is part of every row, so several
    ad accounts or properties share the table).
    """

    def __init__(self, store: Store, connector: str) -> None:
        self.store = store
        self.connector = connector

    async def load(
        self,
        report: str,
        source: TabularSource,
        spec: Any,
        *,
        backfill_days: int = 365,
        today: date | None = None,
    ) -> LoadResult:
        await self.store.ensure_tables([ReportRow])
        today = today or date.today()
        if source.day_column(spec) is None:
            table = await source.read_table(spec, None, None)
            written = await self._replace(report, table, None, None)
            return LoadResult(rows=written)

        last = await self._last_day(report)
        since = (
            last - timedelta(days=source.revision_days)
            if last
            else today - timedelta(days=backfill_days)
        )
        until = today - timedelta(days=1)
        if since > until:
            return LoadResult(rows=0)

        written = 0
        start = since
        while start <= until:
            end = min(start + timedelta(days=CHUNK_DAYS - 1), until)
            table = await source.read_table(spec, start, end)
            written += await self._replace(report, table, start, end)
            _logger.info(f"{self.connector}/{report}: loaded {start}..{end}")
            start = end + timedelta(days=1)
        return LoadResult(rows=written, since=since, until=until)

    async def _last_day(self, report: str) -> date | None:
        async with self.store.session() as session:
            last = await session.scalar(
                select(func.max(ReportRow.day)).where(
                    ReportRow.connector == self.connector, ReportRow.report == report
                )
            )
        return last if isinstance(last, date) else None

    async def _replace(
        self, report: str, table: ReportTable, since: date | None, until: date | None
    ) -> int:
        """Swap what the table holds for `report` over `since`..`until` (all of it when
        `None`) for `table`'s rows, in one transaction."""
        records = [self._record(report, table, row) for row in table.rows]
        async with self.store.session() as session:
            gone = delete(ReportRow).where(
                ReportRow.connector == self.connector, ReportRow.report == report
            )
            if since is not None and until is not None:
                gone = gone.where(ReportRow.day >= since, ReportRow.day <= until)
            await session.execute(gone)
            if records:
                await session.execute(insert(ReportRow), records)
            await session.commit()
        return len(records)

    def _record(self, report: str, table: ReportTable, row: list[str]) -> dict[str, Any]:
        data = dict(zip(table.headers, row, strict=True))
        day = parse_day(data[table.day_column]) if table.day_column else None
        return {"connector": self.connector, "report": report, "day": day, "data": data}
