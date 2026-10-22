"""Drive file payloads as this SDK's `Item`, and how a Google-native file is downloaded."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from data_sources.connectors.google_drive.client import (
    FOLDER_MIME,
    GOOGLE_APPS_PREFIX,
    SHORTCUT_MIME,
)
from data_sources.core.exceptions import ConfigurationError
from data_sources.core.models import Hash, HashAlgorithm, Item, ItemType

#: A Google-native type -> `(mime type, extension)` it is exported as, since it has no bytes
#: of its own. `options.export_formats` overrides or adds entries.
DEFAULT_EXPORTS: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
}

_HASH_FIELDS = (
    ("md5Checksum", HashAlgorithm.MD5),
    ("sha1Checksum", HashAlgorithm.SHA1),
    ("sha256Checksum", HashAlgorithm.SHA256),
)

Exports = Mapping[str, tuple[str, str]]


def export_formats(overrides: Any) -> dict[str, tuple[str, str]]:
    """`DEFAULT_EXPORTS` with `options.export_formats` applied — each entry a Google-native
    type mapped to `{"mime_type": ..., "extension": ...}`."""
    exports = dict(DEFAULT_EXPORTS)
    if not overrides:
        return exports
    if not isinstance(overrides, Mapping):
        raise ConfigurationError("google_drive options.export_formats must be a mapping")
    for google_mime, target in overrides.items():
        if not (
            isinstance(target, Mapping)
            and isinstance(target.get("mime_type"), str)
            and isinstance(target.get("extension"), str)
        ):
            raise ConfigurationError(
                f"google_drive options.export_formats[{google_mime!r}] needs "
                "'mime_type' and 'extension'"
            )
        extension = target["extension"]
        exports[str(google_mime)] = (
            target["mime_type"],
            extension if extension.startswith(".") else f".{extension}",
        )
    return exports


def is_document(raw: Mapping[str, Any], exports: Exports) -> bool:
    """Whether `raw` is a file that can be read: not a folder or shortcut, and — when it's
    Google-native — one with an export format (a Form or a Site has none)."""
    mime = str(raw.get("mimeType", ""))
    if mime in (FOLDER_MIME, SHORTCUT_MIME):
        return False
    return not mime.startswith(GOOGLE_APPS_PREFIX) or mime in exports


def to_item(raw: Mapping[str, Any], parent_path: str, exports: Exports) -> Item:
    """`raw` as an `Item` whose `path` is its parent folder relative to the root. A
    Google-native file is described as what it downloads as: the export's mime type, and
    its extension on the name."""
    mime = str(raw.get("mimeType", ""))
    name = str(raw.get("name", ""))
    export = exports.get(mime)
    if export is not None and not name.lower().endswith(export[1]):
        name += export[1]
    is_folder = mime == FOLDER_MIME

    metadata: dict[str, Any] = {"web_url": raw.get("webViewLink"), "google_mime_type": mime}
    if raw.get("driveId"):
        metadata["drive_id"] = raw["driveId"]

    parents = raw.get("parents") or []
    return Item(
        id=str(raw["id"]),
        name=name,
        type=ItemType.FOLDER if is_folder else ItemType.FILE,
        path=parent_path,
        parent_id=str(parents[0]) if parents else None,
        size=int(raw["size"]) if raw.get("size") else None,
        mime_type=None if is_folder else (export[0] if export else mime or None),
        hashes=[
            Hash(algorithm=algorithm, value=str(raw[key]))
            for key, algorithm in _HASH_FIELDS
            if raw.get(key)
        ],
        created_at=parse_datetime(raw.get("createdTime")),
        modified_at=parse_datetime(raw.get("modifiedTime")),
        metadata=metadata,
    )


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
