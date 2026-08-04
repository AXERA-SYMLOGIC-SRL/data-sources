from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter

from data_sources.core.connector import Connector
from data_sources.web.router import build_router


def build_connectors_router(connectors: Mapping[str, Connector]) -> APIRouter:
    """Aggregate one router per connector, mounted under `/{name}`.

    `connectors` maps a mount name (typically the provider name) to a configured,
    already-`connect()`-ed `Connector` instance. Mount the result into your own
    FastAPI app, e.g.:

        app.include_router(build_connectors_router(connectors), prefix="/connectors")
    """
    root = APIRouter()
    for name, connector in connectors.items():
        root.include_router(build_router(connector, prefix=f"/{name}"))
    return root
