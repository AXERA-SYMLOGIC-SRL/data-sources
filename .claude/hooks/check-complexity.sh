#!/usr/bin/env bash
# After Claude writes a Python file: the same ratchet as the pre-commit hook
# (backend/scripts/check_complexity.py) — only a function that passed the limit,
# or grew past what it was at HEAD, is reported. A file edited from a shell
# command is not seen here; the pre-commit hook holds those.

set -uo pipefail

INPUT=$(cat)

FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

if [[ -z "$FILE_PATH" || "$FILE_PATH" != *.py || ! -f "$FILE_PATH" ]]; then
    exit 0
fi

cd "$CLAUDE_PROJECT_DIR/backend" || exit 0
if ! OUTPUT=$(poetry run python scripts/check_complexity.py "$FILE_PATH" 2>&1); then
    echo "$OUTPUT" >&2
    exit 2
fi

exit 0
