"""Rendering report rows (analytics and ads connectors) as CSV."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable


def to_csv(rows: Iterable[Iterable[str]]) -> bytes:
    """`rows` as UTF-8 CSV text (CRLF line ends, fields quoted as needed)."""
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    return out.getvalue().encode()
