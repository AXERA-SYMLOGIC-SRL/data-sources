from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
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

#: Same drive, but with no subfolder named in the sharing URL — the connector is
#: scoped to the drive's own root.
ROOT_SHARE_URL = (
    "https://contoso.sharepoint.com/sites/Finance/_layouts/15/AllItems.aspx"
    "?id=/sites/Finance/Shared Documents"
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

    async def create_subscription(
        self,
        resource: str,
        notification_url: str,
        expiration: datetime,
        client_state: str | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def list_subscriptions(self) -> AsyncIterator[dict[str, Any]]:
        raise NotImplementedError

    async def renew_subscription(
        self, subscription_id: str, expiration: datetime
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def delete_subscription(self, subscription_id: str) -> None:
        raise NotImplementedError

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
    async def test_item_path_strips_drive_id_and_root_anchor(self) -> None:
        """`Item.path` is relative to the configured root — never the raw Graph
        `/drives/{id}/root:/...` prefix, which isn't client-facing (see `_relative_path`)."""
        connector, fake_client = make_connector()
        raws = [
            _drive_item("1", "at-root.pdf"),
            _drive_item("2", "nested.pdf", parent_path="/drives/drive1/root:/Reports/Q1/Archive"),
        ]

        async def fake_get_delta(
            delta_url: str,
        ) -> AsyncIterator[tuple[dict[str, Any], str | None]]:
            for raw in raws:
                yield raw, None

        fake_client.get_delta = fake_get_delta  # type: ignore[method-assign,assignment]

        items = [item async for item in connector.list(recursive=True)]

        assert [item.path for item in items] == ["", "Archive"]

    @pytest.mark.asyncio
    async def test_item_path_strips_root_colon_when_scoped_to_drive_root(self) -> None:
        """A connector scoped to the drive's own root (no subfolder in the sharing
        URL) anchors on the literal `root:` segment instead of a folder name."""
        connector, fake_client = make_connector(url=ROOT_SHARE_URL)
        raws = [
            _drive_item("1", "at-root.pdf", parent_path="/drives/drive1/root:"),
            _drive_item("2", "nested.pdf", parent_path="/drives/drive1/root:/Invoices"),
        ]

        async def fake_get_delta(
            delta_url: str,
        ) -> AsyncIterator[tuple[dict[str, Any], str | None]]:
            for raw in raws:
                yield raw, None

        fake_client.get_delta = fake_get_delta  # type: ignore[method-assign,assignment]

        items = [item async for item in connector.list(recursive=True)]

        assert [item.path for item in items] == ["", "Invoices"]

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


async def _make_store_connector(
    tmp_path: Path,
) -> tuple[SharePointConnector, FakeSharepointClient, Any]:
    """Like `make_connector`, but store-backed — needed for the webhook methods that
    persist the generated `clientState` secret rather than just talking to Graph."""
    store = await init_store(StoreConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'sharepoint.db'}"))
    connector = await init_connector(
        ConnectorConfig(provider="sharepoint", name="finance-q1", options={"url": SHARE_URL}),
        store,
    )
    assert isinstance(connector, SharePointConnector)
    fake_client = FakeSharepointClient()
    connector._client = cast(SharepointClient, fake_client)
    connector._site_id = "site1"
    connector._drive_id = "drive1"
    return connector, fake_client, store


def _fake_create_subscription(
    seen: dict[str, Any],
) -> Any:
    async def fake_create_subscription(
        resource: str,
        notification_url: str,
        expiration: datetime,
        client_state: str | None = None,
    ) -> dict[str, Any]:
        seen["resource"] = resource
        seen["notification_url"] = notification_url
        seen["client_state"] = client_state
        return {
            "id": "sub-1",
            "resource": resource,
            "notificationUrl": notification_url,
            "expirationDateTime": "2026-02-01T00:00:00Z",
            "clientState": client_state,
        }

    return fake_create_subscription


class TestWebhooks:
    @pytest.mark.asyncio
    async def test_create_webhook_generates_and_persists_a_client_state(
        self, tmp_path: Path
    ) -> None:
        connector, fake_client, store = await _make_store_connector(tmp_path)
        try:
            seen: dict[str, Any] = {}
            fake_client.create_subscription = _fake_create_subscription(seen)  # type: ignore[method-assign]

            subscription = await connector.create_webhook("https://example.com/webhooks")

            assert seen["resource"] == "/drives/drive1/root"
            assert seen["notification_url"] == "https://example.com/webhooks"
            assert seen["client_state"]  # generated by the SDK, not the caller
            assert subscription.id == "sub-1"
            assert subscription.client_state == seen["client_state"]
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_verify_webhook_notification_accepts_matching_state_rejects_tampered(
        self, tmp_path: Path
    ) -> None:
        connector, fake_client, store = await _make_store_connector(tmp_path)
        try:
            seen: dict[str, Any] = {}
            fake_client.create_subscription = _fake_create_subscription(seen)  # type: ignore[method-assign]
            subscription = await connector.create_webhook("https://example.com/webhooks")

            valid = {
                "value": [
                    {"subscriptionId": subscription.id, "clientState": subscription.client_state}
                ]
            }
            assert await connector.verify_webhook_notification(valid) is True

            tampered = {
                "value": [{"subscriptionId": subscription.id, "clientState": "not-the-secret"}]
            }
            assert await connector.verify_webhook_notification(tampered) is False

            empty: dict[str, Any] = {"value": []}
            assert await connector.verify_webhook_notification(empty) is False
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_verify_webhook_notification_rejects_before_any_subscription_exists(
        self, tmp_path: Path
    ) -> None:
        connector, _fake_client, store = await _make_store_connector(tmp_path)
        try:
            payload = {"value": [{"subscriptionId": "sub-1", "clientState": "whatever"}]}
            assert await connector.verify_webhook_notification(payload) is False
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_delete_webhook_clears_persisted_state(self, tmp_path: Path) -> None:
        connector, fake_client, store = await _make_store_connector(tmp_path)
        try:
            seen: dict[str, Any] = {}
            fake_client.create_subscription = _fake_create_subscription(seen)  # type: ignore[method-assign]
            subscription = await connector.create_webhook("https://example.com/webhooks")

            deleted = []

            async def fake_delete_subscription(subscription_id: str) -> None:
                deleted.append(subscription_id)

            fake_client.delete_subscription = fake_delete_subscription  # type: ignore[method-assign]

            await connector.delete_webhook(subscription.id)

            assert deleted == [subscription.id]
            payload = {
                "value": [
                    {"subscriptionId": subscription.id, "clientState": subscription.client_state}
                ]
            }
            assert await connector.verify_webhook_notification(payload) is False
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_delete_webhook_ignores_a_subscription_this_connector_never_tracked(
        self, tmp_path: Path
    ) -> None:
        connector, fake_client, store = await _make_store_connector(tmp_path)
        try:
            seen: dict[str, Any] = {}
            fake_client.create_subscription = _fake_create_subscription(seen)  # type: ignore[method-assign]
            subscription = await connector.create_webhook("https://example.com/webhooks")

            async def fake_delete_subscription(subscription_id: str) -> None:
                pass

            fake_client.delete_subscription = fake_delete_subscription  # type: ignore[method-assign]

            await connector.delete_webhook("some-other-subscription")

            payload = {
                "value": [
                    {"subscriptionId": subscription.id, "clientState": subscription.client_state}
                ]
            }
            assert await connector.verify_webhook_notification(payload) is True
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_renew_webhook_delegates_to_client(self) -> None:
        connector, fake_client = make_connector()
        seen: dict[str, Any] = {}

        async def fake_renew_subscription(
            subscription_id: str, expiration: datetime
        ) -> dict[str, Any]:
            seen["subscription_id"] = subscription_id
            return {
                "id": subscription_id,
                "resource": "/drives/drive1/root",
                "notificationUrl": "https://example.com/webhooks",
                "expirationDateTime": "2026-03-01T00:00:00Z",
            }

        fake_client.renew_subscription = fake_renew_subscription  # type: ignore[method-assign]

        subscription = await connector.renew_webhook("sub-1")

        assert seen["subscription_id"] == "sub-1"
        assert subscription.expiration == datetime(2026, 3, 1, tzinfo=UTC)
        assert subscription.client_state is None

    @pytest.mark.asyncio
    async def test_list_webhooks_filters_to_this_connectors_drive(self) -> None:
        connector, fake_client = make_connector()

        async def fake_list_subscriptions() -> AsyncIterator[dict[str, Any]]:
            for raw in (
                {
                    "id": "sub-1",
                    "resource": "/drives/drive1/root",
                    "notificationUrl": "https://example.com/webhooks",
                    "expirationDateTime": "2026-02-01T00:00:00Z",
                },
                {
                    "id": "sub-2",
                    "resource": "/drives/other-drive/root",
                    "notificationUrl": "https://example.com/webhooks",
                    "expirationDateTime": "2026-02-01T00:00:00Z",
                },
            ):
                yield raw

        fake_client.list_subscriptions = fake_list_subscriptions  # type: ignore[method-assign,assignment]

        subscriptions = await connector.list_webhooks()

        assert [s.id for s in subscriptions] == ["sub-1"]


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

    @pytest.mark.asyncio
    async def test_create_subscription_posts_resource_and_expiration(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url.path == "/v1.0/subscriptions"
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "id": "sub-1",
                    "resource": "/drives/drive1/root",
                    "notificationUrl": "https://example.com/webhooks",
                    "expirationDateTime": "2026-02-01T00:00:00Z",
                    "clientState": "secret",
                },
            )

        client = SharepointClient(
            FakeCredential(),
            ["scope"],
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        result = await client.create_subscription(
            "/drives/drive1/root",
            "https://example.com/webhooks",
            datetime(2026, 2, 1, tzinfo=UTC),
            "secret",
        )

        assert captured["body"] == {
            "changeType": "updated",
            "resource": "/drives/drive1/root",
            "notificationUrl": "https://example.com/webhooks",
            "expirationDateTime": "2026-02-01T00:00:00Z",
            "clientState": "secret",
        }
        assert result["id"] == "sub-1"

    @pytest.mark.asyncio
    async def test_create_subscription_omits_client_state_when_not_given(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "id": "sub-1",
                    "resource": "/drives/drive1/root",
                    "notificationUrl": "https://example.com/webhooks",
                    "expirationDateTime": "2026-02-01T00:00:00Z",
                },
            )

        client = SharepointClient(
            FakeCredential(),
            ["scope"],
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        await client.create_subscription(
            "/drives/drive1/root",
            "https://example.com/webhooks",
            datetime(2026, 2, 1, tzinfo=UTC),
        )

        assert "clientState" not in captured["body"]

    @pytest.mark.asyncio
    async def test_renew_subscription_patches_expiration(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "PATCH"
            assert request.url.path == "/v1.0/subscriptions/sub-1"
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200, json={"id": "sub-1", "expirationDateTime": "2026-03-01T00:00:00Z"}
            )

        client = SharepointClient(
            FakeCredential(),
            ["scope"],
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        result = await client.renew_subscription("sub-1", datetime(2026, 3, 1, tzinfo=UTC))

        assert captured["body"] == {"expirationDateTime": "2026-03-01T00:00:00Z"}
        assert result["id"] == "sub-1"

    @pytest.mark.asyncio
    async def test_delete_subscription_sends_delete_request(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["path"] = request.url.path
            return httpx.Response(204)

        client = SharepointClient(
            FakeCredential(),
            ["scope"],
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        await client.delete_subscription("sub-1")

        assert seen == {"method": "DELETE", "path": "/v1.0/subscriptions/sub-1"}

    @pytest.mark.asyncio
    async def test_delete_subscription_raises_not_found_on_404(self) -> None:
        client = SharepointClient(
            FakeCredential(),
            ["scope"],
            http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        )

        with pytest.raises(NotFoundError):
            await client.delete_subscription("gone")

    @pytest.mark.asyncio
    async def test_list_subscriptions_paginates(self) -> None:
        pages = {
            "https://graph.microsoft.com/v1.0/subscriptions": httpx.Response(
                200,
                json={
                    "value": [{"id": "sub-1"}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/subscriptions?page=2",
                },
            ),
            "https://graph.microsoft.com/v1.0/subscriptions?page=2": httpx.Response(
                200, json={"value": [{"id": "sub-2"}]}
            ),
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return pages[str(request.url)]

        client = SharepointClient(
            FakeCredential(),
            ["scope"],
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        results = [item async for item in client.list_subscriptions()]

        assert [item["id"] for item in results] == ["sub-1", "sub-2"]
