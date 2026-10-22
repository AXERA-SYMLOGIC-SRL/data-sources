"""Writing into a Drive folder: creating folders along a path and uploading a file under a
name, as `ConflictBehavior` says when the name is taken.

Drive lets two files in a folder share a name, so "taken" is decided here, by looking — not
by Drive refusing. A folder of that name takes it too.
"""

from __future__ import annotations

import mimetypes
from typing import Any

from data_sources.connectors.google_drive.client import GOOGLE_APPS_PREFIX, GoogleDriveClient
from data_sources.connectors.google_drive.items import Exports, to_item
from data_sources.core.exceptions import ConflictError, DataSourceError
from data_sources.core.models import ConflictBehavior, Item
from data_sources.core.paths import check_name, join, relative_path, renamed

#: How many `name (n)` candidates a rename tries before giving up.
_MAX_RENAMES = 1000


async def ensure_folder(
    client: GoogleDriveClient, root_id: str, drive_id: str | None, path: str
) -> tuple[dict[str, Any], str]:
    """The folder at relative `path` and the path of the folder holding it — each missing
    segment created; a segment two folders answer to is refused as ambiguous."""
    folder = await client.get_file(root_id)
    parent_path = ""
    walked = ""
    for part in [segment for segment in relative_path(path).split("/") if segment]:
        found = await client.find_child_folders(str(folder["id"]), part, drive_id)
        if len(found) > 1:
            raise DataSourceError(f"folder name {part!r} in {path!r} is ambiguous")
        folder = found[0] if found else await client.create_folder(str(folder["id"]), part)
        parent_path, walked = walked, join(walked, part)
    return folder, parent_path


async def upload(
    client: GoogleDriveClient,
    root_id: str,
    drive_id: str | None,
    exports: Exports,
    target: tuple[str | None, str],
    content: bytes,
    mime_type: str | None,
    on_conflict: ConflictBehavior,
) -> Item:
    """Write `content` as the file `target` (`(folder path, name)`) — see
    `Connector.upload`."""
    folder_path, name = relative_path(target[0]), check_name(target[1])
    folder, _ = await ensure_folder(client, root_id, drive_id, folder_path)
    folder_id = str(folder["id"])
    mime = mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"

    taken = await client.find_children_named(folder_id, name, drive_id, folders=None)
    if not taken:
        raw = await client.create_file(folder_id, name, mime, content)
    elif on_conflict is ConflictBehavior.REPLACE:
        raw = await client.update_content(_replaceable(taken, name), mime, content)
    elif on_conflict is ConflictBehavior.RENAME:
        free = await _free_name(client, folder_id, drive_id, name)
        raw = await client.create_file(folder_id, free, mime, content)
    else:
        raise ConflictError(f"{join(folder_path, name)!r} already exists")
    return to_item(raw, folder_path, exports)


def _replaceable(taken: list[dict[str, Any]], name: str) -> str:
    """The id of the one plain file `taken` holds — a folder, a Google-native file (it has
    no bytes to write over) or two files of that name can't be replaced."""
    if len(taken) != 1:
        raise ConflictError(f"more than one item is named {name!r}; can't tell which to replace")
    mime = str(taken[0].get("mimeType", ""))
    if mime.startswith(GOOGLE_APPS_PREFIX):
        raise ConflictError(f"{name!r} is a folder or a Google-native file; can't write over it")
    return str(taken[0]["id"])


async def _free_name(
    client: GoogleDriveClient, folder_id: str, drive_id: str | None, name: str
) -> str:
    for n in range(1, _MAX_RENAMES + 1):
        candidate = renamed(name, n)
        if not await client.find_children_named(folder_id, candidate, drive_id, folders=None):
            return candidate
    raise ConflictError(f"no free name for {name!r} after {_MAX_RENAMES} tries")
