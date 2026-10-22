"""Paths relative to a connector's root folder: joining them, checking a caller's path stays
inside, and matching `excluded_paths` by whole segments."""

from __future__ import annotations

from collections.abc import Sequence

from data_sources.core.exceptions import NotFoundError


def join(path: str, name: str) -> str:
    """`name` inside the folder at relative `path` (`""` being the root)."""
    return f"{path}/{name}" if path else name


def relative_path(path: str | None) -> str:
    """A caller's folder path, relative to the root, normalized (`""` for the root) —
    refused with `NotFoundError` when any segment is `.` or `..`, so it can't lead out."""
    segments = [segment for segment in (path or "").split("/") if segment]
    if any(segment in (".", "..") for segment in segments):
        raise NotFoundError(f"path {path!r} must stay inside the connected folder")
    return "/".join(segments)


def is_excluded(path: str, excluded: Sequence[str]) -> bool:
    """Whether the folder at relative `path` is, or sits inside, one of `excluded` —
    matched by whole segments, so `Archive` excludes `Archive/2025` but not `Archive2025`."""
    return any(
        path == prefix or path.startswith(f"{prefix}/")
        for prefix in (excluded_path.strip("/") for excluded_path in excluded)
        if prefix
    )
