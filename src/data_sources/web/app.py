from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from fastapi import APIRouter

from data_sources.core.connector import Connector
from data_sources.web.router import build_router


def build_connectors_router(
    connectors: Mapping[str, Connector],
    *,
    on_webhook_notification: Callable[[Connector, dict[str, Any]], Awaitable[None]] | None = None,
) -> APIRouter:
    """Aggregate one router per connector, mounted under `/{name}`.

    `connectors` maps a mount name (typically the provider name) to a configured,
    already-`connect()`-ed `Connector` instance. Mount the result into your own
    FastAPI app, e.g.:

        app.include_router(build_connectors_router(connectors), prefix="/connectors")

    `on_webhook_notification`, if given, is shared across every connector here — see
    `build_router` for what it's handed and when. A caller that needs to treat
    connectors differently can branch on the `Connector` it's passed (e.g. by
    `connector.provider` or `connector.config.name`).
    """
    root = APIRouter()
    for name, connector in connectors.items():
        root.include_router(
            build_router(
                connector,
                prefix=f"/{name}",
                on_webhook_notification=on_webhook_notification,
            )
        )
    return root
