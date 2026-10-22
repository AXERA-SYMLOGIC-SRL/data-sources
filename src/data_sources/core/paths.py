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


def check_name(name: str) -> str:
    """`name` as one file or folder name — refused (`ValueError`) when empty, `.`/`..`, or
    holding a `/`, which would place it somewhere other than the folder it's written to."""
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise ValueError(f"{name!r} is not a single file or folder name")
    return name


def renamed(name: str, n: int) -> str:
    """`name` with ` (n)` before its extension — `report.pdf` → `report (2).pdf`."""
    stem, dot, extension = name.rpartition(".")
    if not dot or not stem:
        return f"{name} ({n})"
    return f"{stem} ({n}).{extension}"


def is_excluded(path: str, excluded: Sequence[str]) -> bool:
    """Whether the folder at relative `path` is, or sits inside, one of `excluded` —
    matched by whole segments, so `Archive` excludes `Archive/2025` but not `Archive2025`."""
    return any(
        path == prefix or path.startswith(f"{prefix}/")
        for prefix in (excluded_path.strip("/") for excluded_path in excluded)
        if prefix
    )
