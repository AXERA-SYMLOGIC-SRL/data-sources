import builtins
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from data_sources.config import ConnectorConfig
from data_sources.core import (
    Connector,
    Item,
    ItemType,
    Permission,
    PermissionPrincipalType,
    PermissionRole,
)
from data_sources.web import build_router
from data_sources.web.app import build_connectors_router


class WebDummyConnector(Connector):
    """Dummy `Connector`. `verify_webhook_notification` trusts every payload by default —
    `options["accept_webhooks"]` flips that, to exercise the router's reject path — since
    this dummy isn't backed by a real subscription secret to check against."""

    provider = "web-dummy"
    supports_permissions = True
    supports_webhooks = True

    def __init__(self, config: ConnectorConfig) -> None:
        super().__init__(config)
        self._accept_webhooks = bool(config.options.get("accept_webhooks", True))

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

    async def verify_webhook_notification(self, payload: dict[str, Any]) -> bool:
        return self._accept_webhooks

    async def close(self) -> None:
        pass


def _client(
    connector: Connector,
    *,
    on_webhook_notification: Any = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(build_router(connector, on_webhook_notification=on_webhook_notification))
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


def test_webhook_notifies_callback_with_connector_and_payload() -> None:
    connector = WebDummyConnector(ConnectorConfig(provider="web-dummy"))
    received: list[tuple[Connector, dict[str, Any]]] = []

    async def on_webhook_notification(conn: Connector, payload: dict[str, Any]) -> None:
        received.append((conn, payload))

    client = _client(connector, on_webhook_notification=on_webhook_notification)

    response = client.post("/webhooks", json={"resource": "changed"})

    assert response.status_code == 202
    assert received == [(connector, {"resource": "changed"})]


def test_webhook_without_callback_just_acknowledges() -> None:
    client = _client(WebDummyConnector(ConnectorConfig(provider="web-dummy")))

    response = client.post("/webhooks", json={})

    assert response.status_code == 202


def test_webhook_rejected_when_verification_fails() -> None:
    client = _client(
        WebDummyConnector(ConnectorConfig(provider="web-dummy", options={"accept_webhooks": False}))
    )

    response = client.post("/webhooks", json={})

    assert response.status_code == 401


def test_webhook_validation_handshake_echoes_token_as_plain_text() -> None:
    client = _client(WebDummyConnector(ConnectorConfig(provider="web-dummy")))

    response = client.post("/webhooks?validationToken=abc123")

    assert response.status_code == 200
    assert response.text == "abc123"
    assert response.headers["content-type"].startswith("text/plain")


def test_build_connectors_router_mounts_by_name() -> None:
    connector = WebDummyConnector(ConnectorConfig(provider="web-dummy"))
    app = FastAPI()
    app.include_router(build_connectors_router({"web-dummy": connector}))
    client = TestClient(app)

    response = client.get("/web-dummy/items")

    assert response.status_code == 200
    assert response.json()[0]["name"] == "file.txt"
