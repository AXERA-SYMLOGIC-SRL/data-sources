"""Splitting a period into the ranges a provider takes per request."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta


def windows(since: date, until: date, days: int) -> Iterator[tuple[date, date]]:
    """Consecutive inclusive ranges of at most `days` days covering `since..until`."""
    if days < 1:
        raise ValueError("days must be at least 1")
    start = since
    while start <= until:
        end = min(until, start + timedelta(days=days - 1))
        yield start, end
        start = end + timedelta(days=1)


def each_day(since: date, until: date) -> Iterator[date]:
    """Every day of `since..until`, for providers queried a day at a time."""
    for start, _ in windows(since, until, 1):
        yield start
