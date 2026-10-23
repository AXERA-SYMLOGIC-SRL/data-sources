#!/usr/bin/env python3
"""PostToolUse hook: keep backend modules under the line ceiling (ratchet vs HEAD).

Runs after every Write/Edit/MultiEdit. For a ``backend/app/**/*.py`` file, the
limit is ``max(500, lines at git HEAD)``:

- a file at or under 500 lines may not grow past 500;
- a file already over 500 (legacy) may shrink or stay put, but not grow;
- a new file (not in HEAD) gets the plain 500 limit.

Over the limit → exit 2 with the reason on stderr, which Claude Code feeds back to
the model so it splits the module before moving on. Anything else → exit 0.
Deterministic: counts physical lines (same as ``wc -l``), no heuristics.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

LIMIT = 500
SCOPE = ("backend", "app")


def _line_count(text: str) -> int:
    return text.count("\n") + (0 if text.endswith("\n") or not text else 1)


def _head_line_count(repo: Path, rel: Path) -> int:
    """Lines of ``rel`` at git HEAD, or 0 if it isn't tracked there."""
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"HEAD:{rel.as_posix()}"],
        capture_output=True,
        text=True,
    )
    return _line_count(result.stdout) if result.returncode == 0 else 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path")
    if not file_path or not file_path.endswith(".py"):
        return 0

    repo = Path(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ".").resolve()
    path = Path(file_path).resolve()
    try:
        rel = path.relative_to(repo)
    except ValueError:
        return 0
    if rel.parts[: len(SCOPE)] != SCOPE or not path.is_file():
        return 0

    current = _line_count(path.read_text(encoding="utf-8", errors="replace"))
    baseline = _head_line_count(repo, rel)
    limit = max(LIMIT, baseline)
    if current <= limit:
        return 0

    if baseline > LIMIT:
        why = (
            f"{rel} is {current} lines; it was already over the {LIMIT}-line ceiling at HEAD "
            f"({baseline} lines), so it may shrink but not grow. Bring it back to "
            f"{baseline} lines or fewer."
        )
    else:
        why = f"{rel} is {current} lines, over the {LIMIT}-line ceiling for backend modules."

    print(
        f"{why}\n"
        "Split it by cohesion: find a group of functions/classes that share one purpose and "
        "have few callers elsewhere (e.g. validation, a store's queries, a prompt-building "
        "step), move them to their own well-named module, and leave the orchestration entry "
        "point here. Don't dump leftovers into a generic helpers/utils file, and don't "
        "compress code (joining lines, deleting docstrings) to get under the limit.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
