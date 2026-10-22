"""Page tokens and the default, provider-agnostic way of paging a listing."""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import AsyncIterator
from typing import Any

from data_sources.core.exceptions import InvalidPageTokenError
from data_sources.core.models import Item, ItemPage


def encode_token(state: dict[str, Any]) -> str:
    """`state` as an opaque, URL-safe page token."""
    return base64.urlsafe_b64encode(json.dumps(state, separators=(",", ":")).encode()).decode()


def decode_token(token: str, kind: str) -> dict[str, Any]:
    """The state in a token `encode_token` made for a listing of `kind` — refused
    (`InvalidPageTokenError`) when it isn't one."""
    try:
        state = json.loads(base64.urlsafe_b64decode(token.encode()))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidPageTokenError("page token is not one this connector issued") from exc
    if not isinstance(state, dict) or state.get("kind") != kind:
        raise InvalidPageTokenError("page token belongs to another kind of listing")
    return state


def check_page_size(page_size: int) -> None:
    if page_size < 1:
        raise ValueError(f"page_size must be at least 1, got {page_size}")


def matches(item: Item, search: str | None) -> bool:
    """Whether `item`'s name contains `search`, case-insensitively (always, without one)."""
    return not search or search.casefold() in item.name.casefold()


async def page_by_offset(
    items: AsyncIterator[Item], page_size: int, page_token: str | None, search: str | None
) -> ItemPage:
    """One page of `items` (those matching `search`), skipping the ones earlier pages
    returned — the token is an offset into the matching items."""
    check_page_size(page_size)
    offset = 0
    if page_token is not None:
        offset = decode_token(page_token, "offset").get("offset", 0)
        if not isinstance(offset, int) or offset < 0:
            raise InvalidPageTokenError("page token carries no valid offset")

    page: list[Item] = []
    index = 0
    try:
        async for item in items:
            if not matches(item, search):
                continue
            if index >= offset:
                if len(page) == page_size:
                    return ItemPage(
                        items=page,
                        next_page_token=encode_token(
                            {"kind": "offset", "offset": offset + page_size}
                        ),
                    )
                page.append(item)
            index += 1
    finally:
        # Stopping early leaves `items` suspended; close it now rather than whenever it's
        # collected, so its connections and streams are released.
        aclose = getattr(items, "aclose", None)
        if aclose is not None:
            await aclose()
    return ItemPage(items=page)
