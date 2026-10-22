from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from data_sources import ConnectorConfig
from data_sources.core import Connector, InvalidPageTokenError, Item, ItemType
from data_sources.core.paging import decode_token, encode_token


class CountingConnector(Connector):
    """A connector whose `list()` yields `count` files and notes whether it was closed
    before it finished — the default `list_page` pages through it."""

    provider = "counting"

    def __init__(self, config: ConnectorConfig, count: int = 7) -> None:
        super().__init__(config)
        self.count = count
        self.closed_early = False

    async def connect(self) -> None:
        pass

    async def validate(self) -> bool:
        return True

    async def list(
        self, path: str | None = None, *, recursive: bool = False
    ) -> AsyncIterator[Item]:
        finished = False
        try:
            for n in range(self.count):
                name = f"invoice-{n}.pdf" if n % 2 else f"note-{n}.txt"
                yield Item(id=str(n), name=name, type=ItemType.FILE)
            finished = True
        finally:
            if not finished:
                self.closed_early = True

    async def get_metadata(self, item_id: str) -> Item:
        return Item(id=item_id, name=item_id, type=ItemType.FILE)

    async def download(self, item: Item) -> AsyncIterator[bytes]:
        yield b""

    async def close(self) -> None:
        pass


def _connector(count: int = 7) -> CountingConnector:
    return CountingConnector(ConnectorConfig(provider="counting"), count)


async def test_default_list_page_pages_through_list() -> None:
    connector = _connector()
    ids: list[str] = []
    token: str | None = None
    while True:
        page = await connector.list_page(page_size=3, page_token=token)
        assert len(page.items) <= 3
        ids += [item.id for item in page.items]
        token = page.next_page_token
        if token is None:
            break

    assert ids == [str(n) for n in range(7)]


async def test_default_list_page_filters_by_search_before_paging() -> None:
    connector = _connector()

    first = await connector.list_page(page_size=2, search="INVOICE")
    second = await connector.list_page(
        page_size=2, search="INVOICE", page_token=first.next_page_token
    )

    assert [item.id for item in first.items] == ["1", "3"]
    assert [item.id for item in second.items] == ["5"]
    assert second.next_page_token is None


async def test_a_page_that_stops_early_closes_the_listing() -> None:
    connector = _connector()

    page = await connector.list_page(page_size=2)

    assert page.next_page_token is not None
    assert connector.closed_early


async def test_an_exact_last_page_has_no_next_token() -> None:
    page = await _connector(count=2).list_page(page_size=2)

    assert len(page.items) == 2 and page.next_page_token is None


@pytest.mark.parametrize(
    "token",
    [
        "garbage",
        encode_token({"kind": "google_drive"}),
        encode_token({"kind": "offset", "offset": -1}),
        encode_token({"kind": "offset", "offset": "3"}),
    ],
)
async def test_default_list_page_refuses_tokens_it_did_not_issue(token: str) -> None:
    with pytest.raises(InvalidPageTokenError):
        await _connector().list_page(page_token=token)


async def test_page_size_must_be_positive() -> None:
    with pytest.raises(ValueError):
        await _connector().list_page(page_size=0)


def test_tokens_round_trip_and_name_their_kind() -> None:
    token = encode_token({"kind": "offset", "offset": 4})

    assert decode_token(token, "offset") == {"kind": "offset", "offset": 4}
    with pytest.raises(InvalidPageTokenError):
        decode_token(token, "sharepoint")
