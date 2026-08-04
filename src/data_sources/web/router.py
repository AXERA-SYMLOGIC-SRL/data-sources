from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from data_sources.core.connector import Connector
from data_sources.core.exceptions import NotFoundError
from data_sources.core.models import Change, Item, Permission


def build_router(connector: Connector, *, prefix: str = "") -> APIRouter:
    """Build an `APIRouter` exposing generic item and webhook endpoints for `connector`.

    Routes are derived entirely from the `Connector` interface: read endpoints are
    always present, `/webhooks` is added only when `connector.supports_webhooks`,
    and `/items/{item_id}/permissions` only when `connector.supports_permissions`.
    """
    router = APIRouter(prefix=prefix, tags=[connector.provider])

    @router.get("/items", response_model=list[Item])
    async def list_items(
        path: str | None = Query(default=None),
        recursive: bool = Query(default=False),
    ) -> list[Item]:
        return [item async for item in connector.list(path, recursive=recursive)]

    @router.get("/items/{item_id}", response_model=Item)
    async def get_item(item_id: str) -> Item:
        try:
            return await connector.get_metadata(item_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/items/{item_id}/download")
    async def download_item(item_id: str) -> StreamingResponse:
        try:
            item = await connector.get_metadata(item_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        return StreamingResponse(
            connector.download(item),
            media_type=item.mime_type or "application/octet-stream",
        )

    if connector.supports_permissions:

        @router.get("/items/{item_id}/permissions", response_model=list[Permission])
        async def get_permissions(item_id: str) -> list[Permission]:
            try:
                item = await connector.get_metadata(item_id)
            except NotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

            return await connector.get_permissions(item)

    if connector.supports_webhooks:

        @router.post("/webhooks", status_code=202, response_model=list[Change])
        async def receive_webhook(request: Request) -> list[Change]:
            await request.body()
            if not connector.supports_sync:
                return []
            return [change async for change in connector.sync()]

    return router
