"""Structural fidelity: does the emitted report have the pages, visuals and field bindings a faithful
migration of each Tableau sheet would have?

No rendering and no data: every check reads the project files, so this runs anywhere, including CI.
Each check has a stable id (``visual-type``, ``Y[Sales:sum]``, ``slicer[Order Date]``…) so an
expectation file can mark it as a known gap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from t2pbi.fidelity.expectations import (
    AGGREGATIONS,
    DATE_LEVELS,
    FieldSpec,
    SheetExpectation,
    WorkbookExpectations,
)
from t2pbi.fidelity.project import Page, Project, Visual

Outcome = Literal["pass", "fail", "gap", "fixed"]


@dataclass(frozen=True)
class CheckResult:
    sheet: str
    check: str
    passed: bool
    detail: str = ""
    known_gap: str | None = None

    @property
    def outcome(self) -> Outcome:
        """pass/fail as usual; ``gap`` = failing as documented; ``fixed`` = a known gap now passes."""

        if self.known_gap is None:
            return "pass" if self.passed else "fail"
        return "fixed" if self.passed else "gap"


def score_structural(expected: WorkbookExpectations, project: Project) -> list[CheckResult]:
    """Run every structural check for every expected sheet."""

    results: list[CheckResult] = []
    titles = {s.name: s.title for s in expected.sheets if s.title}
    for sheet in expected.sheets:
        for check, passed, detail in _sheet_checks(sheet, project, titles):
            results.append(
                CheckResult(sheet.name, check, passed, detail, sheet.known_gaps.get(check))
            )
        # value[...] gaps belong to the numeric layer, which checks them itself.
        structural_gaps = {k for k in sheet.known_gaps if not k.startswith("value[")}
        unknown = structural_gaps - {r.check for r in results if r.sheet == sheet.name}
        if unknown:
            raise ValueError(f"sheet {sheet.name!r}: known_gaps name unknown checks {sorted(unknown)}")
    return results


def _sheet_checks(sheet: SheetExpectation, project: Project, titles: dict[str, str]):
    page = project.page(sheet.page_name)
    yield "page", page is not None, "" if page else f"no page named {sheet.page_name!r}"

    if sheet.kind == "tooltip":
        yield "tooltip-page", bool(page and page.page_type == "Tooltip"), f"type={page and page.page_type}"
        yield (
            "hidden",
            bool(page and page.visibility == "HiddenInViewMode"),
            f"visibility={page and page.visibility}",
        )

    if sheet.kind == "dashboard":
        yield from _dashboard_checks(sheet, page, titles)
        return

    visual = _main_visual(page)
    if sheet.title is not None:
        actual = visual.title if visual else None
        yield "title", _title_matches(actual, sheet.title), f"got {actual!r}, want {sheet.title!r}…"
    if sheet.visual_types:
        actual = visual.visual_type if visual else None
        yield "visual-type", actual in sheet.visual_types, f"got {actual}, want one of {sheet.visual_types}"
    for role, specs in sheet.roles.items():
        bound = visual.roles.get(role, []) if visual else []
        yield from _field_checks(role, specs, bound, project)
    if sheet.fields:
        bound = [f for fields in visual.roles.values() for f in fields] if visual else []
        yield from _field_checks("fields", sheet.fields, bound, project)


def _dashboard_checks(sheet: SheetExpectation, page: Page | None, titles: dict[str, str]):
    visuals = page.visuals if page else []
    shown = [v.title for v in visuals]
    for name in sheet.contains:
        # A visual is found by its sheet's name or by its sheet's expected title, so this checks
        # presence whether or not titles have been migrated yet (that is the `title` check's job).
        found = any(t == name or _title_matches(t, titles.get(name)) for t in shown)
        yield f"contains[{name}]", found, "" if found else f"visual titles: {shown}"
    slicers = [f for v in visuals if v.visual_type.endswith("licer") for f in v.roles.get("Values", [])]
    for spec in sheet.slicers:
        found = any(matches(spec, f, None) for f in slicers)
        yield f"slicer[{spec}]", found, "" if found else "no slicer on that field"


def _field_checks(label: str, specs: list[FieldSpec], bound: list[dict], project: Project):
    unmatched = list(bound)
    for spec in specs:
        hit = next((f for f in unmatched if matches(spec, f, project)), None)
        if hit is not None:
            unmatched.remove(hit)
        yield f"{label}[{spec}]", hit is not None, "" if hit else f"bound: {[describe(f) for f in bound]}"
    yield (
        f"{label}:no-extras",
        not unmatched,
        "" if not unmatched else f"unexpected: {[describe(f) for f in unmatched]}",
    )


def _title_matches(actual: str | None, expected: str | None) -> bool:
    """The title shows the expected static text; a dynamic suffix (filter values) may follow."""

    return bool(actual and expected) and actual.startswith(expected)


def _main_visual(page: Page | None) -> Visual | None:
    if page is None:
        return None
    return next((v for v in page.visuals if not v.visual_type.endswith("licer")), None)


# --------------------------------------------------------------------------------------------
# Matching one expected field against one PBIR field expression
# --------------------------------------------------------------------------------------------


def matches(spec: FieldSpec, expr: dict, project: Project | None) -> bool:
    """Whether the PBIR field expression ``expr`` is what ``spec`` asks for."""

    q = spec.qualifier
    if q is None:
        return _column(expr) == spec.name
    if q == "measure":
        return "Measure" in expr and expr["Measure"].get("Property") == spec.name
    if q in AGGREGATIONS:
        agg = expr.get("Aggregation")
        return bool(agg) and agg.get("Function") == AGGREGATIONS[q] and _column(agg["Expression"]) == spec.name
    if q in DATE_LEVELS:
        level = expr.get("HierarchyLevel")
        return bool(level) and level.get("Level", "").lower() == q and spec.name in _properties(level)
    if q == "rowcount":
        return _is_row_count(spec.name, expr, project)
    raise ValueError(f"unhandled qualifier {q!r}")


def describe(expr: dict) -> str:
    """Short human form of a field expression, for failure details."""

    if "Measure" in expr:
        return f"{expr['Measure'].get('Property')}:measure"
    if "Aggregation" in expr:
        agg = expr["Aggregation"]
        name = {v: k for k, v in AGGREGATIONS.items()}.get(agg.get("Function"), agg.get("Function"))
        return f"{_column(agg['Expression'])}:{name}"
    if "HierarchyLevel" in expr:
        return f"{'/'.join(_properties(expr))}:{expr['HierarchyLevel'].get('Level')}"
    return str(_column(expr))


def _column(expr: dict) -> str | None:
    col = expr.get("Column")
    return col.get("Property") if isinstance(col, dict) else None


def _entity(expr: dict) -> str | None:
    for kind in ("Column", "Measure"):
        node = expr.get(kind)
        if isinstance(node, dict):
            return node.get("Expression", {}).get("SourceRef", {}).get("Entity")
    return None


def _properties(node) -> list[str]:
    """Every ``Property`` value nested anywhere in a field expression."""

    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Property" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_properties(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_properties(item))
    return found


def _is_row_count(table: str, expr: dict, project: Project | None) -> bool:
    """A count of the table's rows: a Count aggregation over one of its columns, or a measure whose
    DAX is ``COUNTROWS('<table>')``."""

    agg = expr.get("Aggregation")
    if agg and agg.get("Function") in (AGGREGATIONS["count"], AGGREGATIONS["countd"]):
        return _entity(agg["Expression"]) == table
    measure = expr.get("Measure")
    if measure and project is not None:
        _, dax = project.measures.get(measure.get("Property", ""), ("", ""))
        pattern = rf"^\s*COUNTROWS\s*\(\s*'?{re.escape(table)}'?\s*\)\s*$"
        return re.match(pattern, dax, re.IGNORECASE) is not None
    return False
