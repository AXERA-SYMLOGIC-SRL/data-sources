"""Walking a provider's pages. A repeated token or link, or more pages than `max_pages`,
raises `invalid` instead of looping."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from data_sources.reports.errors import Problem

Body = dict[str, Any]


async def token_pages(
    fetch: Callable[[str | None], Awaitable[Body]],
    *,
    invalid: Problem,
    token_key: str = "nextPageToken",
    max_pages: int = 10_000,
) -> AsyncIterator[Body]:
    """Pages that name the next one by token. `fetch(None)` is the first page."""
    token: str | None = None
    seen: set[str] = set()
    while True:
        body = await fetch(token)
        yield body
        token = body.get(token_key) or None
        if token is None:
            return
        if not isinstance(token, str) or token in seen or len(seen) >= max_pages:
            raise invalid.error()
        seen.add(token)


async def link_pages(
    fetch: Callable[[str | None], Awaitable[Body]],
    next_link: Callable[[Body], str | None],
    *,
    invalid: Problem,
    max_pages: int = 10_000,
) -> AsyncIterator[Body]:
    """Pages that link to the next one. `fetch(None)` is the first page, `fetch(url)` a later
    one."""
    link: str | None = None
    seen: set[str] = set()
    while True:
        body = await fetch(link)
        yield body
        link = next_link(body)
        if not link:
            return
        if link in seen or len(seen) >= max_pages:
            raise invalid.error()
        seen.add(link)
