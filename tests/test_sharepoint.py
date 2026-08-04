from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from data_sources.config.schema import AuthConfig, ConnectorConfig, StoreConfig
from data_sources.connectors.sharepoint.client import SharepointClient
from data_sources.connectors.sharepoint.connector import SharePointConnector
from data_sources.connectors.sharepoint.models import SharePointItemRecord, SharePointSyncState
from data_sources.core.exceptions import (
    AuthenticationError,
    ConfigurationError,
    NotFoundError,
)
from data_sources.core.exceptions import (
    ConnectionError as DataSourceConnectionError,
)
from data_sources.core.models import ChangeType, Item, ItemType, SyncCursor
from data_sources.store import init_connector, init_store

SHARE_URL = (
    "https://contoso.sharepoint.com/sites/Finance/_layouts/15/AllItems.aspx"
    "?id=/sites/Finance/Shared Documents/Reports/Q1"
)


class FakeCredential:
    def __init__(self) -> None:
        self.closed = False

    async def get_token(self, *scopes: str) -> Any:
        class _Token:
            token = "fake-token"

        return _Token()

    async def close(self) -> None:
        self.closed = True


def _drive_item(
    item_id: str,
    name: str,
    *,
    parent_path: str = "/drives/drive1/root:/Reports/Q1",
    created: str = "2026-01-01T00:00:00Z",
    modified: str = "2026-01-01T00:00:00Z",
    folder: bool = False,
    deleted: bool = False,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": item_id,
        "name": name,
        "parentReference": {"id": "parent1", "path": parent_path},
        "createdDateTime": created,
        "lastModifiedDateTime": modified,
        "webUrl": f"https://contoso.sharepoint.com/{name}",
    }
    if folder:
        item["folder"] = {"childCount": 0}
    else:
        item["size"] = 123
        item["file"] = {"mimeType": "application/pdf", "hashes": {"quickXorHash": "abc123"}}
    if deleted:
        item["deleted"] = {"state": "deleted"}
    return item


class FakeSharepointClient:
    """Duck-types the subset of `SharepointClient` the connector calls, so connector
    tests don't need real Graph API/Azure credentials."""

    def __init__(self) -> None:
        self.closed = False
        self.downloaded: list[tuple[str, str]] = []

    def delta_url(self, site_id: str, drive_id: str, item_path: str) -> str:
        return f"https://graph/delta/{site_id}/{drive_id}/{item_path}"

    async def get_delta(self, delta_url: str) -> AsyncIterator[tuple[dict[str, Any], str | None]]:
        raise NotImplementedError

    async def list_children(self, drive_id: str, item_path: str) -> AsyncIterator[dict[str, Any]]:
        raise NotImplementedError

    async def get_item_by_id(self, drive_id: str, item_id: str) -> dict[str, Any]:
        return _drive_item(item_id, "file.pdf")

    async def download(self, drive_id: str, item_id: str) -> AsyncIterator[bytes]:
        self.downloaded.append((drive_id, item_id))
        for chunk in (b"chunk-1", b"chunk-2"):
            yield chunk

    async def close(self) -> None:
        self.closed = True


def make_connector(**options: Any) -> tuple[SharePointConnector, FakeSharepointClient]:
    config = ConnectorConfig(provider="sharepoint", options={"url": SHARE_URL, **options})
    connector = SharePointConnector(config)
    fake_client = FakeSharepointClient()
    connector._client = cast(SharepointClient, fake_client)
    connector._site_id = "site1"
    connector._drive_id = "drive1"
    return connector, fake_client


class TestParseSharingUrl:
    def test_splits_drive_name_and_path(self) -> None:
        drive_name, path = SharepointClient.parse_sharing_url(SHARE_URL)

        assert drive_name == "Shared Documents"
        assert path == "Reports/Q1"

    def test_raises_on_url_without_id_param(self) -> None:
        with pytest.raises(ValueError):
            SharepointClient.parse_sharing_url("https://contoso.sharepoint.com/sites/Finance")


class TestConnectorConstruction:
    def test_requires_url_option(self) -> None:
        with pytest.raises(ConfigurationError):
            SharePointConnector(ConnectorConfig(provider="sharepoint"))

    @pytest.mark.asyncio
    async def test_connect_requires_auth_credentials(self) -> None:
        config = ConnectorConfig(
            provider="sharepoint",
            options={"url": SHARE_URL},
            auth=AuthConfig(type="client_credentials", credentials={"tenant_id": "t"}),
        )
        connector = SharePointConnector(config)

        with pytest.raises(ConfigurationError):
            await connector.connect()

    def test_client_property_requires_connect(self) -> None:
        connector = SharePointConnector(
            ConnectorConfig(provider="sharepoint", options={"url": SHARE_URL})
        )

        with pytest.raises(DataSourceConnectionError):
            _ = connector.client


class TestList:
    @pytest.mark.asyncio
    async def test_recursive_skips_folders_deleted_and_excluded(self) -> None:
        connector, fake_client = make_connector(excluded_paths=["Archive"])
        raws = [
            _drive_item("1", "keep.pdf"),
            _drive_item("2", "folder", folder=True),
            _drive_item("3", "gone.pdf", deleted=True),
            _drive_item("4", "old.pdf", parent_path="/drives/drive1/root:/Reports/Q1/Archive"),
        ]

        async def fake_get_delta(
            delta_url: str,
        ) -> AsyncIterator[tuple[dict[str, Any], str | None]]:
            for raw in raws:
                yield raw, None

        fake_client.get_delta = fake_get_delta  # type: ignore[method-assign,assignment]

        items = [item async for item in connector.list(recursive=True)]

        assert [item.id for item in items] == ["1"]
        assert items[0].type == ItemType.FILE
        assert items[0].mime_type == "application/pdf"

    @pytest.mark.asyncio
    async def test_non_recursive_uses_list_children(self) -> None:
        connector, fake_client = make_connector()

        async def fake_list_children(
            drive_id: str, item_path: str
        ) -> AsyncIterator[dict[str, Any]]:
            assert drive_id == "drive1"
            assert item_path == "Reports/Q1"
            yield _drive_item("1", "a.pdf")

        fake_client.list_children = fake_list_children  # type: ignore[method-assign,assignment]

        items = [item async for item in connector.list(recursive=False)]

        assert [item.id for item in items] == ["1"]


class TestSync:
    @pytest.mark.asyncio
    async def test_classifies_created_updated_deleted_and_carries_cursor(self) -> None:
        connector, fake_client = make_connector()
        pages = [
            (_drive_item("1", "new.pdf"), None),
            (
                _drive_item(
                    "2",
                    "changed.pdf",
                    created="2026-01-01T00:00:00Z",
                    modified="2026-01-02T00:00:00Z",
                ),
                None,
            ),
            (_drive_item("3", "gone.pdf", deleted=True), "https://graph/delta-link-final"),
        ]

        async def fake_get_delta(
            delta_url: str,
        ) -> AsyncIterator[tuple[dict[str, Any], str | None]]:
            for raw, delta_link in pages:
                yield raw, delta_link

        fake_client.get_delta = fake_get_delta  # type: ignore[method-assign,assignment]

        changes = [change async for change in connector.sync()]

        assert [c.type for c in changes] == [
            ChangeType.CREATED,
            ChangeType.UPDATED,
            ChangeType.DELETED,
        ]
        assert changes[0].cursor is None
        assert changes[2].cursor == SyncCursor(token="https://graph/delta-link-final")
        assert changes[2].item is None

    @pytest.mark.asyncio
    async def test_resumes_from_passed_cursor(self) -> None:
        connector, fake_client = make_connector()
        seen_urls = []

        async def fake_get_delta(
            delta_url: str,
        ) -> AsyncIterator[tuple[dict[str, Any], str | None]]:
            seen_urls.append(delta_url)
            return
            yield

        fake_client.get_delta = fake_get_delta  # type: ignore[method-assign,assignment]

        _ = [c async for c in connector.sync(SyncCursor(token="https://graph/resume-here"))]

        assert seen_urls == ["https://graph/resume-here"]


@pytest.mark.asyncio
async def test_download_delegates_to_client(tmp_path: Path) -> None:
    connector, fake_client = make_connector()
    item = await connector.get_metadata("42")

    chunks = [chunk async for chunk in connector.download(item)]

    assert chunks == [b"chunk-1", b"chunk-2"]
    assert fake_client.downloaded == [("drive1", "42")]


@pytest.mark.asyncio
async def test_sync_cursor_persists_across_load_and_commit(tmp_path: Path) -> None:
    store = await init_store(StoreConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'sharepoint.db'}"))
    try:
        connector = await init_connector(
            ConnectorConfig(provider="sharepoint", name="finance-q1", options={"url": SHARE_URL}),
            store,
        )
        assert isinstance(connector, SharePointConnector)

        assert await connector._load_cursor() is None

        await connector._commit_cursor(SyncCursor(token="https://graph/delta-1"))
        assert await connector._load_cursor() == SyncCursor(token="https://graph/delta-1")

        await connector._commit_cursor(SyncCursor(token="https://graph/delta-2"))
        assert await connector._load_cursor() == SyncCursor(token="https://graph/delta-2")

        async with store.session() as session:
            rows = (await session.execute(SharePointSyncState.__table__.select())).all()
            assert len(rows) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_get_item_resolves_a_saved_item_without_a_live_call(tmp_path: Path) -> None:
    store = await init_store(StoreConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'sharepoint.db'}"))
    try:
        connector = await init_connector(
            ConnectorConfig(provider="sharepoint", name="finance-q1", options={"url": SHARE_URL}),
            store,
        )
        assert isinstance(connector, SharePointConnector)

        assert await connector.get_item("1") is None

        item = Item(id="1", name="report.pdf", type=ItemType.FILE)
        await connector._save_item(item)
        assert await connector.get_item("1") == item

        updated = Item(id="1", name="report-v2.pdf", type=ItemType.FILE)
        await connector._save_item(updated)
        assert await connector.get_item("1") == updated

        await connector._delete_item("1")
        assert await connector.get_item("1") is None

        async with store.session() as session:
            rows = (await session.execute(SharePointItemRecord.__table__.select())).all()
            assert rows == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sync_in_background_indexes_items_by_id(tmp_path: Path) -> None:
    store = await init_store(StoreConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'sharepoint.db'}"))
    try:
        connector = await init_connector(
            ConnectorConfig(provider="sharepoint", name="finance-q1", options={"url": SHARE_URL}),
            store,
        )
        assert isinstance(connector, SharePointConnector)

        fake_client = FakeSharepointClient()
        connector._client = cast(SharepointClient, fake_client)
        connector._site_id = "site1"
        connector._drive_id = "drive1"

        pages = [
            (_drive_item("1", "keep.pdf"), None),
            (_drive_item("2", "gone.pdf", deleted=True), "https://graph/delta-link-final"),
        ]

        async def fake_get_delta(
            delta_url: str,
        ) -> AsyncIterator[tuple[dict[str, Any], str | None]]:
            for raw, delta_link in pages:
                yield raw, delta_link

        fake_client.get_delta = fake_get_delta  # type: ignore[method-assign,assignment]

        async def on_change(change: Any) -> None:
            pass

        await connector.sync_in_background(on_change)

        kept = await connector.get_item("1")
        assert kept is not None
        assert kept.name == "keep.pdf"
        assert await connector.get_item("2") is None
    finally:
        await store.close()


class TestSharepointClientAgainstGraph:
    @pytest.mark.asyncio
    async def test_get_site_id(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1.0/sites/contoso.sharepoint.com:/sites/Finance"
            return httpx.Response(200, json={"id": "site1"})

        client = SharepointClient(
            FakeCredential(),
            ["scope"],
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        site_id = await client.get_site_id("https://contoso.sharepoint.com/sites/Finance/whatever")

        assert site_id == "site1"

    @pytest.mark.asyncio
    async def test_get_delta_paginates_and_surfaces_delta_link_on_last_page_only(self) -> None:
        pages = {
            "https://graph/delta/start": httpx.Response(
                200,
                json={
                    "value": [{"id": "1"}],
                    "@odata.nextLink": "https://graph/delta/page2",
                },
            ),
            "https://graph/delta/page2": httpx.Response(
                200,
                json={"value": [{"id": "2"}], "@odata.deltaLink": "https://graph/delta/resume"},
            ),
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return pages[str(request.url)]

        client = SharepointClient(
            FakeCredential(),
            ["scope"],
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        results = [pair async for pair in client.get_delta("https://graph/delta/start")]

        assert [item["id"] for item, _ in results] == ["1", "2"]
        assert results[0][1] is None
        assert results[1][1] == "https://graph/delta/resume"

    @pytest.mark.asyncio
    async def test_retries_on_429_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        attempts = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["count"] += 1
            if attempts["count"] == 1:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(200, json={"value": []})

        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr("data_sources.connectors.sharepoint.client.asyncio.sleep", no_sleep)

        client = SharepointClient(
            FakeCredential(),
            ["scope"],
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        result = await client.get_json("https://graph/whatever")

        assert result == {"value": []}
        assert attempts["count"] == 2

    @pytest.mark.asyncio
    async def test_404_raises_not_found_error(self) -> None:
        client = SharepointClient(
            FakeCredential(),
            ["scope"],
            http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        )

        with pytest.raises(NotFoundError):
            await client.get_json("https://graph/missing")

    @pytest.mark.asyncio
    async def test_401_raises_authentication_error(self) -> None:
        client = SharepointClient(
            FakeCredential(),
            ["scope"],
            http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401))),
        )

        with pytest.raises(AuthenticationError):
            await client.get_json("https://graph/secure")
