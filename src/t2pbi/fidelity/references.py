"""Reference images: the user's screenshots of each Tableau sheet, for visual fidelity checks.

The screenshots folder has one subfolder per worksheet or dashboard, named after it, holding one
or more images of that sheet: the whole view, and if useful close-ups (a legend, a hovered
tooltip). A future upload wizard writes the same layout::

    screenshots/
      Sales by Category/
        view.png
        legend.png
      Tooltip_ Profit Ratio by City/
        view.png

People (and file systems: ``:`` isn't allowed on Windows) name folders loosely, so names are
matched ignoring case, spaces and punctuation: ``Tooltip_ Profit Ratio by City`` is the folder of
"Tooltip: Profit Ratio by City".

A sheet without a folder, or with an empty one, isn't compared with a picture at all. The
thumbnails Tableau embeds in a workbook are not used: at 192px or less, titles, labels and date
levels can't be read, so they gave the judge too little to score on (DEVLOG, 2026-11-27).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}


def normalize_name(name: str) -> str:
    """Lower-case letters and digits only: the key two names must share to match."""

    return re.sub(r"[^0-9a-z]", "", name.lower())


@dataclass
class Screenshots:
    """A screenshots folder matched to a workbook's sheets."""

    images: dict[str, list[Path]] = field(default_factory=dict)  # sheet -> its images, by file name
    ambiguous: dict[str, list[str]] = field(default_factory=dict)  # sheet -> the folders that all match it
    unmatched: list[str] = field(default_factory=list)  # folders named after no sheet
    loose: list[str] = field(default_factory=list)  # images not in any sheet's folder


def match_screenshots(folder: str | Path | None, sheets: Iterable[str]) -> Screenshots:
    """Match the subfolders of ``folder`` to ``sheets`` by name and collect their images.

    A sheet that two folders match gets neither: which one is meant would be a guess. Folders
    named after no sheet and images lying directly in ``folder`` are listed, since they're most
    likely misnamed or misplaced.
    """

    found = Screenshots()
    if folder is None or not Path(folder).is_dir():
        return found
    entries = sorted(Path(folder).iterdir())
    subfolders = [p for p in entries if p.is_dir()]
    names = list(sheets)
    for sheet in names:
        hits = [p for p in subfolders if normalize_name(p.name) == normalize_name(sheet)]
        if len(hits) > 1:
            found.ambiguous[sheet] = [p.name for p in hits]
        elif hits:
            images = sorted(p for p in hits[0].iterdir() if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES)
            if images:
                found.images[sheet] = images
    keys = {normalize_name(sheet) for sheet in names}
    found.unmatched = [p.name for p in subfolders if normalize_name(p.name) not in keys]
    found.loose = [p.name for p in entries if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES]
    return found
