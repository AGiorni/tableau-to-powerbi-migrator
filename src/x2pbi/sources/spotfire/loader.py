"""Load a Spotfire analysis file into whatever the parser needs.

Not yet implemented. A ``.dxp`` is a container (unlike Tableau's plain-XML ``.twb``), so this
module owns the container handling and hands the parser an already-opened structure — mirroring
how ``sources/tableau/loader.py`` hands the Tableau parser an ``lxml`` tree.

The file-level checks below (existence, extension) are real, so a bad path fails the same way it
does for Tableau rather than falling through to a confusing container error.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

_EXTENSIONS = {".dxp"}


@dataclass(frozen=True)
class LoadedAnalysis:
    """A parsed analysis plus the provenance the parser needs downstream.

    ``document`` is deliberately untyped until the container format is settled — it will hold
    whatever structure ``parse_analysis`` reads (an element tree, a dict of extracted parts, ...).
    """

    source_name: str
    document: Any


def load_analysis(path: str | Path) -> LoadedAnalysis:
    """Load a ``.dxp`` analysis file.

    Raises ``FileNotFoundError`` for a missing path, ``ValueError`` for an unsupported extension,
    and ``NotImplementedError`` until the container reader is written.
    """

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Spotfire file not found: {p}")

    suffix = p.suffix.lower()
    if suffix not in _EXTENSIONS:
        raise ValueError(f"Unsupported Spotfire file type '{suffix}'. Expected .dxp.")

    raise NotImplementedError(
        "Reading Spotfire .dxp containers is not implemented yet. Implement the container "
        "reader here and return a LoadedAnalysis; see sources/spotfire/parser.py for the IR "
        "the parser must then produce."
    )
