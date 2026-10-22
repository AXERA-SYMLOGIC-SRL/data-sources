"""`list_page` for a Drive folder: one traversal for both a single level and a whole subtree,
its state carried in the page token.

A token names Drive folders, and a caller can hand back anything as a token — so every folder
in it is placed again by its current ancestry (`Ancestry`) before it is read: one that isn't
inside the connected root (forged, or moved out since) is dropped, never listed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from data_sources.connectors.google_drive.client import (
    FOLDER_MIME,
    SHORTCUT_MIME,
    GoogleDriveClient,
)
from data_sources.connectors.google_drive.items import Exports, is_document, to_item
from data_sources.connectors.google_drive.scope import Ancestry
from data_sources.core.exceptions import InvalidPageTokenError
from data_sources.core.models import Item, ItemPage
from data_sources.core.paging import decode_token, encode_token, matches
from data_sources.core.paths import is_excluded, join

_KIND = "google_drive"


@dataclass
class _Walk:
    """Where a listing is: the folder being read (`folder`, its `path`), Drive's token for
    its next page, and the folders still to read (recursive listings only)."""

    start: str
    recursive: bool
    folder: str
    path: str
    drive_token: str | None = None
    queue: list[tuple[str, str]] = field(default_factory=list)

    def token(self) -> str:
        return encode_token(
            {
                "kind": _KIND,
                "start": self.start,
                "recursive": self.recursive,
                "folder": self.folder,
                "path": self.path,
                "drive_token": self.drive_token,
                "queue": [list(entry) for entry in self.queue],
            }
        )

    @classmethod
    def resume(cls, token: str, start: str, recursive: bool) -> _Walk:
        state = decode_token(token, _KIND)
        if state.get("start") != start or state.get("recursive") != recursive:
            raise InvalidPageTokenError("page token belongs to another listing")
        try:
            return cls(
                start=start,
                recursive=recursive,
                folder=str(state["folder"]),
                path=str(state["path"]),
                drive_token=state.get("drive_token"),
                queue=[(str(folder), str(path)) for folder, path in state.get("queue", [])],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidPageTokenError("page token is malformed") from exc

    def advance(self) -> bool:
        """Move on to the next queued folder; `False` when none is left."""
        if not self.queue:
            return False
        self.folder, self.path = self.queue.pop(0)
        self.drive_token = None
        return True


async def list_page(
    client: GoogleDriveClient,
    root_id: str,
    drive_id: str | None,
    start: tuple[str, str],
    *,
    recursive: bool,
    page_size: int,
    page_token: str | None,
    search: str | None,
    excluded: tuple[str, ...],
    exports: Exports,
) -> ItemPage:
    """A page of the listing starting at `start` (`(folder id, path)`): documents — and on
    a single level, subfolders too — whose name contains `search`."""
    folder_id, path = start
    walk = (
        _Walk.resume(page_token, folder_id, recursive)
        if page_token
        else _Walk(start=folder_id, recursive=recursive, folder=folder_id, path=path)
    )
    ancestry = Ancestry(client, root_id)
    items: list[Item] = []

    if not await _placed(walk, ancestry, excluded):
        return ItemPage(items=items)
    while True:
        children, walk.drive_token = await client.children_page(
            walk.folder, drive_id, page_size - len(items), walk.drive_token
        )
        for raw in children:
            item = _take(raw, walk, excluded, exports)
            if item is not None and matches(item, search):
                items.append(item)
        if walk.drive_token is None and not (
            walk.advance() and await _placed(walk, ancestry, excluded)
        ):
            return ItemPage(items=items)
        if len(items) >= page_size:
            return ItemPage(items=items, next_page_token=walk.token())


def _take(
    raw: dict[str, Any], walk: _Walk, excluded: tuple[str, ...], exports: Exports
) -> Item | None:
    """What a child of the folder being read contributes: a document as an item; a folder
    queued to read (recursive) or as an item (single level); nothing otherwise."""
    mime = raw.get("mimeType")
    if mime == SHORTCUT_MIME:
        return None
    if mime == FOLDER_MIME:
        child_path = join(walk.path, str(raw.get("name", "")))
        if is_excluded(child_path, excluded):
            return None
        if walk.recursive:
            walk.queue.append((str(raw["id"]), child_path))
            return None
        return to_item(raw, walk.path, exports)
    return to_item(raw, walk.path, exports) if is_document(raw, exports) else None


async def _placed(walk: _Walk, ancestry: Ancestry, excluded: tuple[str, ...]) -> bool:
    """Settle the walk on a folder inside the root — its path read from Drive, not the
    token — skipping queued ones that aren't (anymore); `False` once none is left."""
    while True:
        path = await ancestry.folder_path(walk.folder)
        if path is not None and not is_excluded(path, excluded):
            walk.path = path
            return True
        if not walk.advance():
            return False
