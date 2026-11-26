"""Hand-reviewed expectations: what each Tableau sheet shows, stated in Power BI terms.

One TOML file per sample workbook in ``samples/expected/``. Each ``[[sheet]]`` describes one
Tableau worksheet or dashboard as a faithful Power BI report would show it — the truth, not what
t2pbi produces today. Checks that t2pbi is known to fail are listed under ``known_gaps`` with the
reason, so the score shows both progress and regressions.

Field specs are short strings:

=====================  ==================================================================
``"Category"``         the column ``Category`` (a grouping field)
``"Sales:sum"``        an implicit aggregation of ``Sales``: sum, avg, count, countd, min,
                       max or median
``"profit ratio:measure"``  the model measure ``profit ratio``
``"Order Date:month"`` the date ``Order Date`` at month level: year, quarter, month or day
``"Orders:rowcount"``  the number of rows of table ``Orders`` (Tableau's "count of table")
=====================  ==================================================================
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from t2pbi.mapping.visual_catalog import VISUAL_CATALOG

# QueryAggregateFunction codes, as emitted by emit/pbir_writer.py (2 = distinct count in the UI).
AGGREGATIONS = {"sum": 0, "avg": 1, "countd": 2, "min": 3, "max": 4, "count": 5, "median": 6}
DATE_LEVELS = {"year": "Year", "quarter": "Quarter", "month": "Month", "day": "Day"}
_OTHER_QUALIFIERS = {"measure", "rowcount"}

SheetKind = Literal["worksheet", "tooltip", "dashboard"]


@dataclass(frozen=True)
class FieldSpec:
    """One expected field. ``qualifier`` is None for a plain column."""

    name: str
    qualifier: str | None = None

    @classmethod
    def parse(cls, text: str) -> FieldSpec:
        name, sep, qualifier = text.rpartition(":")
        known = AGGREGATIONS.keys() | DATE_LEVELS.keys() | _OTHER_QUALIFIERS
        if sep and qualifier in known and name:
            return cls(name=name, qualifier=qualifier)
        return cls(name=text)

    def __str__(self) -> str:
        return f"{self.name}:{self.qualifier}" if self.qualifier else self.name


@dataclass(frozen=True)
class Reference:
    """A number as Tableau displayed it, and how far off a value may be while still matching.

    The tolerance is half a unit of the last digit shown: ``"29"`` accepts 28.5–29.5, ``"15.54%"``
    accepts 0.15535–0.15545. Thousands separators and currency symbols are ignored.
    """

    text: str
    value: float
    tolerance: float

    @classmethod
    def parse(cls, text: str) -> Reference:
        cleaned = re.sub(r"[\s,$€£]", "", text)
        percent = cleaned.endswith("%")
        number = cleaned.rstrip("%")
        decimals = len(number.split(".", 1)[1]) if "." in number else 0
        try:
            value = float(number)
        except ValueError:
            raise ValueError(f"not a number: {text!r}") from None
        tolerance = 0.5 * 10**-decimals
        if percent:
            value, tolerance = value / 100, tolerance / 100
        return cls(text=text, value=value, tolerance=tolerance)

    def accepts(self, actual: float) -> bool:
        return abs(actual - self.value) <= self.tolerance + 1e-12


@dataclass
class SheetExpectation:
    name: str
    kind: SheetKind = "worksheet"
    page: str | None = None
    # The static part of the title Tableau displays; dynamic inserts (filter values) may follow it.
    title: str | None = None
    visual_types: list[str] = field(default_factory=list)
    roles: dict[str, list[FieldSpec]] = field(default_factory=dict)
    fields: list[FieldSpec] = field(default_factory=list)
    contains: list[str] = field(default_factory=list)
    slicers: list[FieldSpec] = field(default_factory=list)
    # Reference numbers as Tableau displays them ("10,194", "15.54%"), per field spec: the
    # sheet's grand total of that field. Checked by fidelity.numeric against the running model.
    values: dict[FieldSpec, str] = field(default_factory=dict)
    known_gaps: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def page_name(self) -> str:
        return self.page or self.name


@dataclass
class WorkbookExpectations:
    workbook: str
    sheets: list[SheetExpectation]


def load_expectations(path: str | Path) -> WorkbookExpectations:
    """Read and sanity-check one expectations file; raises ValueError on authoring mistakes."""

    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    sheets = [_parse_sheet(raw) for raw in data.get("sheet", [])]
    names = [s.name for s in sheets]
    if len(names) != len(set(names)):
        raise ValueError(f"{path}: duplicate sheet names")
    return WorkbookExpectations(workbook=data["workbook"], sheets=sheets)


_SHEET_KEYS = {
    "name", "kind", "page", "title", "visual", "roles", "fields", "contains", "slicers",
    "values", "known_gaps", "notes",
}


def _parse_sheet(raw: dict) -> SheetExpectation:
    unknown = set(raw) - _SHEET_KEYS
    if unknown:
        raise ValueError(f"sheet {raw.get('name')!r}: unknown keys {sorted(unknown)}")
    sheet = SheetExpectation(
        name=raw["name"],
        kind=raw.get("kind", "worksheet"),
        page=raw.get("page"),
        title=raw.get("title"),
        visual_types=list(raw.get("visual", [])),
        roles={role: [FieldSpec.parse(f) for f in specs] for role, specs in raw.get("roles", {}).items()},
        fields=[FieldSpec.parse(f) for f in raw.get("fields", [])],
        contains=list(raw.get("contains", [])),
        slicers=[FieldSpec.parse(f) for f in raw.get("slicers", [])],
        values={FieldSpec.parse(k): str(v) for k, v in raw.get("values", {}).items()},
        known_gaps=dict(raw.get("known_gaps", {})),
        notes=list(raw.get("notes", [])),
    )
    if sheet.kind not in ("worksheet", "tooltip", "dashboard"):
        raise ValueError(f"sheet {sheet.name!r}: unknown kind {sheet.kind!r}")
    for spec, text in sheet.values.items():
        try:
            Reference.parse(text)
        except ValueError as exc:
            raise ValueError(f"sheet {sheet.name!r}: values[{spec}]: {exc}") from None
    for vt in sheet.visual_types:
        if vt not in VISUAL_CATALOG:
            raise ValueError(f"sheet {sheet.name!r}: visual type {vt!r} is not in the visual catalog")
    # Role names come from the catalog, never intuition: each role must exist on some allowed type.
    for role in sheet.roles:
        if not any(role in VISUAL_CATALOG[vt].roles for vt in sheet.visual_types):
            raise ValueError(
                f"sheet {sheet.name!r}: role {role!r} is not a role of any of {sheet.visual_types}"
            )
    return sheet
