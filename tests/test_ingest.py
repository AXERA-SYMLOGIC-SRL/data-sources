from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from data_sources.config.schema import ConnectorConfig, StoreConfig
from data_sources.connectors.google_analytics import GoogleAnalyticsConnector
from data_sources.connectors.google_analytics.models import ReportResult, ReportSpec
from data_sources.connectors.meta_ads import InsightsResult, InsightsSpec, MetaAdsConnector
from data_sources.ingest import ReportLoader, ReportRow, ReportTable, parse_day
from data_sources.store import Store, create_store

TODAY = date(2026, 10, 23)


class FakeSource:
    """A report whose every day has one row; `sessions` is whatever `values` says for that day
    (so a later load can see a day change), and each request is recorded."""

    revision_days = 3

    def __init__(self, daily: bool = True, fmt: str = "%Y%m%d") -> None:
        self.daily = daily
        self.fmt = fmt
        self.values: dict[date, str] = {}
        self.requests: list[tuple[date | None, date | None]] = []

    def day_column(self, spec: Any) -> str | None:
        return "date" if self.daily else None

    async def read_table(self, spec: Any, since: date | None, until: date | None) -> ReportTable:
        self.requests.append((since, until))
        if not self.daily:
            return ReportTable(headers=["country", "sessions"], rows=[["RO", "5"], ["DE", "3"]])
        assert since is not None and until is not None
        days = [since + timedelta(days=n) for n in range((until - since).days + 1)]
        rows = [[d.strftime(self.fmt), self.values.get(d, "1")] for d in days]
        return ReportTable(headers=["date", "sessions"], rows=rows, day_column="date")


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    s = create_store(StoreConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'ingest.db'}"))
    await s.connect()
    yield s
    await s.close()


async def rows(store: Store, **where: str) -> list[ReportRow]:
    async with store.session() as session:
        query = select(ReportRow).order_by(ReportRow.day, ReportRow.id)
        for column, value in where.items():
            query = query.where(getattr(ReportRow, column) == value)
        return list((await session.scalars(query)).all())


def test_parse_day() -> None:
    assert parse_day("20261022") == date(2026, 10, 22)
    assert parse_day("2026-10-22") == date(2026, 10, 22)
    with pytest.raises(ValueError):
        parse_day("(not set)")


async def test_first_load_backfills_up_to_yesterday_in_chunks(store: Store) -> None:
    source = FakeSource()
    result = await ReportLoader(store, "site").load(
        "daily", source, None, backfill_days=70, today=TODAY
    )

    first, last = TODAY - timedelta(days=70), TODAY - timedelta(days=1)
    assert (result.since, result.until, result.rows) == (first, last, 70)
    assert source.requests == [  # 31-day chunks, none reaching today
        (first, first + timedelta(days=30)),
        (first + timedelta(days=31), first + timedelta(days=61)),
        (first + timedelta(days=62), last),
    ]
    stored = await rows(store)
    assert [r.day for r in stored][0] == first and stored[-1].day == last
    assert stored[0].data == {"date": first.strftime("%Y%m%d"), "sessions": "1"}


async def test_later_load_refetches_only_the_revision_window_and_replaces_it(
    store: Store,
) -> None:
    source = FakeSource()
    loader = ReportLoader(store, "site")
    await loader.load("daily", source, None, backfill_days=10, today=TODAY)

    source.requests.clear()
    source.values[TODAY - timedelta(days=2)] = "99"  # the API revised a recent day
    next_day = TODAY + timedelta(days=1)
    result = await loader.load("daily", source, None, backfill_days=10, today=next_day)

    # last day held is TODAY-1; the window starts revision_days before it, ends TODAY
    assert source.requests == [(TODAY - timedelta(days=4), TODAY)]
    assert result.rows == 5
    stored = await rows(store)
    assert len(stored) == 11  # 10 first, + TODAY; the window's days replaced, not duplicated
    assert {r.day: r.data["sessions"] for r in stored}[TODAY - timedelta(days=2)] == "99"


async def test_a_load_with_no_period_to_cover_does_nothing(store: Store) -> None:
    source = FakeSource()

    result = await ReportLoader(store, "site").load(
        "daily", source, None, backfill_days=0, today=TODAY
    )

    assert (result.rows, result.since, result.until) == (0, None, None)
    assert source.requests == [] and await rows(store) == []


async def test_snapshot_report_is_replaced_whole(store: Store) -> None:
    source = FakeSource(daily=False)
    loader = ReportLoader(store, "site")

    await loader.load("countries", source, None, today=TODAY)
    await loader.load("countries", source, None, today=TODAY)

    stored = await rows(store)
    assert len(stored) == 2 and all(r.day is None for r in stored)
    assert source.requests == [(None, None), (None, None)]


async def test_connectors_and_reports_do_not_touch_each_other(store: Store) -> None:
    source = FakeSource()
    next_day = TODAY + timedelta(days=1)
    await ReportLoader(store, "a").load("daily", source, None, backfill_days=3, today=TODAY)
    await ReportLoader(store, "b").load("daily", source, None, backfill_days=3, today=TODAY)
    await ReportLoader(store, "a").load("other", source, None, backfill_days=3, today=TODAY)
    # reloading a/daily replaces only a/daily's revision window (TODAY-4..TODAY)
    await ReportLoader(store, "a").load("daily", source, None, backfill_days=3, today=next_day)

    assert len(await rows(store, connector="a", report="daily")) == 5
    assert len(await rows(store, connector="a", report="other")) == 3
    assert len(await rows(store, connector="b")) == 3


async def test_failed_chunk_leaves_what_was_loaded_and_resumes(store: Store) -> None:
    class Flaky(FakeSource):
        calls = 0

        async def read_table(
            self, spec: Any, since: date | None, until: date | None
        ) -> ReportTable:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("API down")
            return await super().read_table(spec, since, until)

    source = Flaky()
    loader = ReportLoader(store, "site")
    with pytest.raises(RuntimeError):
        await loader.load("daily", source, None, backfill_days=40, today=TODAY)
    assert len(await rows(store)) == 31  # the first chunk stayed

    await loader.load("daily", source, None, backfill_days=40, today=TODAY)
    days = [r.day for r in await rows(store)]
    assert len(days) == len(set(days)) == 40  # resumed from the last day held, no duplicates


async def test_rows_carry_the_data_as_strings(store: Store) -> None:
    await ReportLoader(store, "site").load("countries", FakeSource(daily=False), None)
    stored = await rows(store)
    assert {r.data["country"]: r.data["sessions"] for r in stored} == {"RO": "5", "DE": "3"}


# the connectors' side of the contract


def ga() -> GoogleAnalyticsConnector:
    return GoogleAnalyticsConnector(
        ConnectorConfig(provider="google_analytics", options={"property": "1"})
    )


def meta() -> MetaAdsConnector:
    return MetaAdsConnector(ConnectorConfig(provider="meta_ads", options={"ad_account": "1"}))


async def test_ga_read_table_sets_the_period_and_the_day_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[ReportSpec] = []

    async def fake_run(self: GoogleAnalyticsConnector, spec: ReportSpec) -> ReportResult:
        seen.append(spec)
        return ReportResult(
            headers=["date", "sessions"], metric_types=[], rows=[["20261001", "4"]], row_count=1
        )

    monkeypatch.setattr(GoogleAnalyticsConnector, "run_report", fake_run)
    spec = ReportSpec(dimensions=["date"], metrics=["sessions"])

    table = await ga().read_table(spec, date(2026, 10, 1), date(2026, 10, 31))

    assert (seen[0].date_ranges[0].start_date, seen[0].date_ranges[0].end_date) == (
        "2026-10-01",
        "2026-10-31",
    )
    assert table.day_column == "date" and table.rows == [["20261001", "4"]]
    assert ga().day_column(ReportSpec(dimensions=["country"], metrics=["sessions"])) is None
    await ga().read_table(spec, None, None)
    assert seen[1].date_ranges == spec.date_ranges  # its own period when none is given


async def test_meta_read_table_sets_the_period_and_the_day_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[InsightsSpec] = []

    async def fake_run(self: MetaAdsConnector, spec: InsightsSpec) -> InsightsResult:
        seen.append(spec)
        return InsightsResult(headers=["spend", "date_start"], rows=[["1.5", "2026-10-01"]])

    monkeypatch.setattr(MetaAdsConnector, "run_insights", fake_run)
    spec = InsightsSpec(fields=["spend"], date_preset="last_7d", time_increment=1)

    table = await meta().read_table(spec, date(2026, 10, 1), date(2026, 10, 31))

    assert (seen[0].since, seen[0].until, seen[0].date_preset) == (
        "2026-10-01",
        "2026-10-31",
        None,
    )
    assert table.day_column == "date_start"
    assert meta().day_column(InsightsSpec(fields=["spend"])) is None
    assert meta().revision_days == 28 and ga().revision_days == 3
