"""Reference images: how each Tableau sheet looks, for visual fidelity checks.

The user supplies screenshots once per workbook — one PNG per worksheet or dashboard, named after
it. People name files loosely, so names are matched ignoring case, spaces and punctuation
("Tooltip: Profit Ratio by City" finds ``tooltip profit ratio by city.png``).
"""

from __future__ import annotations

import re
from pathlib import Path

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}


def normalize_name(name: str) -> str:
    """Lower-case letters and digits only: the key two names must share to match."""

    return re.sub(r"[^0-9a-z]", "", name.lower())


def find_reference_image(folder: str | Path, sheet_name: str) -> Path | None:
    """The image in ``folder`` for ``sheet_name``, or None. Raises if two files match."""

    root = Path(folder)
    if not root.is_dir():
        return None
    key = normalize_name(sheet_name)
    hits = [
        p
        for p in root.iterdir()
        if p.suffix.lower() in _IMAGE_SUFFIXES and normalize_name(p.stem) == key
    ]
    if len(hits) > 1:
        raise ValueError(f"several reference images match {sheet_name!r}: {[p.name for p in hits]}")
    return hits[0] if hits else None
