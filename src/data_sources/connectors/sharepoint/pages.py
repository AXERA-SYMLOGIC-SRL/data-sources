"""`list_page` for a SharePoint folder over Graph's own paging — a folder's children for a
single level, the folder's delta feed for a whole subtree.

Graph pages by `@odata.nextLink`, a full URL. Handing that out as the page token would let a
caller send this connector's access token to any URL it forged into one. So a token carries
only the next link's *query string* (Graph's `$skiptoken`/`token`), and the URL is always
rebuilt from the listing's own address — a forged token can't point anywhere else, nor at
another folder. It also carries how far into a Graph page this listing had read, since a
Graph page can hold more than one of ours.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from data_sources.connectors.sharepoint.client import SharepointClient
from data_sources.core.exceptions import InvalidPageTokenError
from data_sources.core.models import Item, ItemPage
from data_sources.core.paging import decode_token, encode_token, matches

_KIND = "sharepoint"


@dataclass
class _Position:
    """Which Graph page of the listing (`query`: its query string, `None` for the first)
    and how many of its entries were already read."""

    path: str
    recursive: bool
    query: str | None = None
    skip: int = 0

    def token(self) -> str:
        return encode_token(
            {
                "kind": _KIND,
                "path": self.path,
                "recursive": self.recursive,
                "query": self.query,
                "skip": self.skip,
            }
        )

    @classmethod
    def resume(cls, token: str, path: str, recursive: bool) -> _Position:
        state = decode_token(token, _KIND)
        if state.get("path") != path or state.get("recursive") != recursive:
            raise InvalidPageTokenError("page token belongs to another listing")
        query, skip = state.get("query"), state.get("skip")
        if (query is not None and not isinstance(query, str)) or not isinstance(skip, int):
            raise InvalidPageTokenError("page token is malformed")
        return cls(path=path, recursive=recursive, query=query, skip=max(skip, 0))


async def list_page(
    client: SharepointClient,
    base_url: str,
    position: tuple[str, bool],
    take: Callable[[dict[str, Any]], Item | None],
    *,
    page_size: int,
    page_token: str | None,
    search: str | None,
) -> ItemPage:
    """A page of the listing at `base_url` (a folder's `children`, or its `delta`):
    the entries `take` turns into items, whose name contains `search`."""
    path, recursive = position
    at = (
        _Position.resume(page_token, path, recursive)
        if page_token
        else _Position(path=path, recursive=recursive)
    )
    items: list[Item] = []
    while True:
        url = f"{base_url}?{at.query}" if at.query else f"{base_url}?$top={page_size}"
        page = await client.get_json(url)
        values: list[dict[str, Any]] = page.get("value", [])
        next_link = page.get("@odata.nextLink")

        for index in range(at.skip, len(values)):
            item = take(values[index])
            if item is not None and matches(item, search):
                items.append(item)
            if len(items) < page_size:
                continue
            if index + 1 < len(values):
                at.skip = index + 1
                return ItemPage(items=items, next_page_token=at.token())
            if next_link:
                return ItemPage(items=items, next_page_token=_next(at, next_link).token())
            return ItemPage(items=items)

        if not next_link:
            return ItemPage(items=items)
        at = _next(at, next_link)


def _next(at: _Position, next_link: str) -> _Position:
    return _Position(path=at.path, recursive=at.recursive, query=urlsplit(next_link).query)
