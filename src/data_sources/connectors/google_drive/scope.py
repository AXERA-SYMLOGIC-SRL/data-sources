"""The connected folder's boundary: which folder a config names, where a folder or file sits
relative to it (read from Drive's *current* parents, never trusted from an earlier read), and
walking everything under it."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import AsyncIterator, Sequence
from typing import Any

from data_sources.connectors.google_drive.client import (
    FOLDER_MIME,
    SHORTCUT_MIME,
    GoogleDriveClient,
)
from data_sources.core.exceptions import DataSourceError, NotFoundError

#: How many folders up an item's ancestry is followed before it's treated as outside.
MAX_DEPTH = 100


def parse_folder(raw: str) -> str | None:
    """The folder id in `raw` — a Drive folder URL (`.../folders/<id>`), a bare id, or the
    literal `root` (the user's My Drive). `None` when it is none of these: a folder is never
    looked up by name, since a name isn't one folder in a Drive."""
    raw = raw.strip()
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)(?:[/?#]|$)", raw)
    if match:
        return match.group(1)
    return raw if raw == "root" or re.fullmatch(r"[a-zA-Z0-9_-]{10,}", raw) else None


def join(path: str, name: str) -> str:
    return f"{path}/{name}" if path else name


def is_excluded(path: str, excluded: Sequence[str]) -> bool:
    """Whether the folder at `path` (relative to the root) is, or sits inside, one of the
    `excluded` folders — matched by whole path segments, so `Archive` doesn't exclude
    `Archive2025`."""
    return any(
        path == prefix or path.startswith(f"{prefix}/")
        for prefix in (excluded_path.strip("/") for excluded_path in excluded)
        if prefix
    )


class Ancestry:
    """Where folders sit relative to the connected root, read from their current parents and
    cached for the length of one operation — a new one per operation, so a folder moved
    since an earlier operation isn't placed where it used to be."""

    def __init__(self, client: GoogleDriveClient, root_id: str) -> None:
        self._client = client
        #: folder id -> its path relative to the root, or `None` when it's outside it.
        self._paths: dict[str, str | None] = {root_id: ""}

    def remember(self, folder_id: str, path: str | None) -> None:
        self._paths[folder_id] = path

    async def folder_path(self, folder_id: str) -> str | None:
        """The path of `folder_id` relative to the root (`""` for the root itself), or `None`
        when it isn't a live folder inside it."""
        chain: list[tuple[str, str]] = []
        current: str | None = folder_id
        result: str | None = None
        while current is not None:
            if current in self._paths:
                result = self._paths[current]
                break
            if len(chain) >= MAX_DEPTH or any(current == seen for seen, _ in chain):
                break
            raw = await self._live_folder(current)
            if raw is None:
                self._paths[current] = None
                break
            chain.append((current, str(raw.get("name", ""))))
            parents = raw.get("parents") or []
            current = str(parents[0]) if parents else None

        for walked_id, name in reversed(chain):
            result = None if result is None else join(result, name)
            self._paths[walked_id] = result
        return self._paths.get(folder_id)

    async def parent_path(self, raw: dict[str, Any]) -> str | None:
        """The path of the folder `raw` sits in, relative to the root — `None` when it isn't
        inside the root at all."""
        for parent in raw.get("parents") or []:
            path = await self.folder_path(str(parent))
            if path is not None:
                return path
        return None

    async def _live_folder(self, folder_id: str) -> dict[str, Any] | None:
        try:
            raw = await self._client.get_file(folder_id)
        except NotFoundError:
            return None
        if raw.get("trashed") or raw.get("mimeType") != FOLDER_MIME:
            return None
        return raw


async def resolve_path(
    client: GoogleDriveClient, root_id: str, path: str, drive_id: str | None
) -> str:
    """The id of the folder at `path` (relative to the root, `/`-separated), resolved one
    exact name at a time — a missing or ambiguous name fails, never widening to the root."""
    current = root_id
    for part in path.strip("/").split("/"):
        if part in ("", ".", ".."):
            raise NotFoundError(f"folder path {path!r} must stay inside the connected folder")
        found = await client.find_child_folders(current, part, drive_id)
        if not found:
            raise NotFoundError(f"no folder {part!r} in the connected folder's {path!r}")
        if len(found) > 1:
            raise DataSourceError(f"folder name {part!r} in {path!r} is ambiguous")
        current = str(found[0]["id"])
    return current


async def walk(
    client: GoogleDriveClient,
    folder_id: str,
    path: str,
    drive_id: str | None,
    excluded: Sequence[str] = (),
) -> AsyncIterator[tuple[dict[str, Any], str]]:
    """Every live file and folder under `folder_id` (whose path is `path`), breadth-first,
    as `(raw, parent_path)` — shortcuts skipped, excluded folders not entered."""
    queue = deque([(folder_id, path)])
    entered = {folder_id}
    while queue:
        current, current_path = queue.popleft()
        async for raw in client.list_children(current, drive_id):
            mime = raw.get("mimeType")
            if mime == SHORTCUT_MIME:
                continue
            if mime == FOLDER_MIME:
                child_path = join(current_path, str(raw.get("name", "")))
                if is_excluded(child_path, excluded) or raw["id"] in entered:
                    continue
                entered.add(raw["id"])
                queue.append((raw["id"], child_path))
            yield raw, current_path
