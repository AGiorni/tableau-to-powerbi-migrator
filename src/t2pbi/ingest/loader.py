"""Load a Tableau workbook file into an XML element tree.

Supports both ``.twb`` (raw XML) and ``.twbx`` (a zip archive containing a single ``.twb`` at
its root plus data extracts, which we ignore for metadata migration).
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

from lxml import etree


@dataclass(frozen=True)
class LoadedWorkbook:
    """A parsed workbook plus provenance needed downstream."""

    source_name: str
    root: etree._Element


def load_workbook(path: str | Path) -> LoadedWorkbook:
    """Load a ``.twb`` or ``.twbx`` file and return its parsed XML root.

    Raises ``FileNotFoundError`` if the path does not exist and ``ValueError`` for unsupported
    extensions or malformed archives.
    """

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Tableau file not found: {p}")

    suffix = p.suffix.lower()
    if suffix == ".twb":
        xml_bytes = p.read_bytes()
    elif suffix == ".twbx":
        xml_bytes = _extract_twb_from_twbx(p)
    else:
        raise ValueError(f"Unsupported Tableau file type '{suffix}'. Expected .twb or .twbx.")

    # ``recover=True`` tolerates the minor XML quirks that appear in real workbooks.
    parser = etree.XMLParser(recover=True, huge_tree=True)
    root = etree.fromstring(xml_bytes, parser=parser)
    if root is None:
        raise ValueError(f"Could not parse XML from {p}")

    return LoadedWorkbook(source_name=p.stem, root=root)


def _extract_twb_from_twbx(path: Path) -> bytes:
    """Return the bytes of the ``.twb`` entry inside a ``.twbx`` archive."""

    with zipfile.ZipFile(path) as archive:
        twb_names = [n for n in archive.namelist() if n.lower().endswith(".twb")]
        if not twb_names:
            raise ValueError(f"No .twb entry found inside archive {path}")
        # A .twbx contains exactly one workbook; pick the shallowest path if several match.
        twb_name = min(twb_names, key=lambda n: n.count("/"))
        return archive.read(twb_name)
