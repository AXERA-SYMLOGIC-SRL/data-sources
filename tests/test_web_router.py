import builtins
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.testclient import TestClient

from data_sources.config import ConnectorConfig
from data_sources.core import (
    Change,
    ChangeType,
    Connector,
    Item,
    ItemType,
    Permission,
    PermissionPrincipalType,
    PermissionRole,
    SyncCursor,
)
from data_sources.web import build_router
from data_sources.web.app import build_connectors_router


class WebDummyConnector(Connector):
    provider = "web-dummy"
    supports_permissions = True
    supports_webhooks = True
    supports_sync = True

    async def connect(self) -> None:
        pass

    async def validate(self) -> bool:
        return True

    async def list(
        self, path: str | None = None, *, recursive: bool = False
    ) -> AsyncIterator[Item]:
        yield Item(id="1", name="file.txt", type=ItemType.FILE)

    async def get_metadata(self, item_id: str) -> Item:
        return Item(id=item_id, name="file.txt", type=ItemType.FILE, mime_type="text/plain")

    async def download(self, item: Item) -> AsyncIterator[bytes]:
        yield b"hello "
        yield b"world"

    async def get_permissions(self, item: Item) -> builtins.list[Permission]:
        return [
            Permission(
                principal="alice@example.com",
                principal_type=PermissionPrincipalType.USER,
                role=PermissionRole.OWNER,
            )
        ]

    async def sync(self, cursor: SyncCursor | None = None) -> AsyncIterator[Change]:
        yield Change(item_id="1", type=ChangeType.UPDATED)

    async def close(self) -> None:
        pass


def _client(connector: Connector) -> TestClient:
    app = FastAPI()
    app.include_router(build_router(connector))
    return TestClient(app)


def test_list_items() -> None:
    client = _client(WebDummyConnector(ConnectorConfig(provider="web-dummy")))

    response = client.get("/items")

    assert response.status_code == 200
    assert response.json()[0]["name"] == "file.txt"


def test_get_item() -> None:
    client = _client(WebDummyConnector(ConnectorConfig(provider="web-dummy")))

    response = client.get("/items/1")

    assert response.status_code == 200
    assert response.json()["id"] == "1"


def test_download_item() -> None:
    client = _client(WebDummyConnector(ConnectorConfig(provider="web-dummy")))

    response = client.get("/items/1/download")

    assert response.status_code == 200
    assert response.content == b"hello world"


def test_get_permissions() -> None:
    client = _client(WebDummyConnector(ConnectorConfig(provider="web-dummy")))

    response = client.get("/items/1/permissions")

    assert response.status_code == 200
    assert response.json()[0]["principal"] == "alice@example.com"


def test_webhook_triggers_sync() -> None:
    client = _client(WebDummyConnector(ConnectorConfig(provider="web-dummy")))

    response = client.post("/webhooks", json={})

    assert response.status_code == 202
    assert response.json()[0]["item_id"] == "1"


def test_build_connectors_router_mounts_by_name() -> None:
    connector = WebDummyConnector(ConnectorConfig(provider="web-dummy"))
    app = FastAPI()
    app.include_router(build_connectors_router({"web-dummy": connector}))
    client = TestClient(app)

    response = client.get("/web-dummy/items")

    assert response.status_code == 200
    assert response.json()[0]["name"] == "file.txt"
