"""Expectations derived from the Tableau workbook itself, for workbooks nobody wrote an oracle for.

At run time a customer's workbook has no hand-reviewed ``samples/expected/*.toml``; this builds
the checkable part of one straight from the Tableau IR, so the structural scorer can tell the
refinement agent what's missing. It states **coverage**, never the mapper's choices:

- every field on the sheet's shelves and encodings (rows, columns, colour, size, text, label,
  tooltip; detail too on maps), with its aggregation or date level, must appear in the visual;
- the title Tableau displays (static part); whether the sheet is a viz-in-tooltip; its sort;
- for dashboards, the sheets they show and the slicers for their filter cards.

It never states a visual type or which role a field belongs in: those are the mapper's decisions,
and copying its rules here would only reproduce its blind spots. How close this gets to a
hand-written oracle is measured against ``samples/expected/Book1.toml`` in the tests.
"""

from __future__ import annotations

import re

from t2pbi.fidelity.expectations import FieldSpec, SheetExpectation, WorkbookExpectations
from t2pbi.ir.tableau_ir import (
    TableauCalculatedField,
    TableauFieldRef,
    TableauRole,
    TableauWorkbook,
    TableauWorksheet,
    strip_brackets,
)

# Tableau column-instance prefixes -> field-spec qualifiers.
_DATE_LEVELS = {"yr": "year", "tyr": "year", "qr": "quarter", "tqr": "quarter",
                "mn": "month", "tmn": "month", "dy": "day", "tdy": "day"}
_AGGREGATIONS = {"sum": "sum", "avg": "avg", "cnt": "count", "cntd": "countd",
                 "min": "min", "max": "max", "median": "median"}
# Tableau's per-table row count shows up as "<Table>_<32 hex digits>" with a count aggregation.
_TABLE_COUNT = re.compile(r"^(.*)_[0-9A-Fa-f]{32}$")
_ENCODINGS = ("color", "size", "text", "label", "tooltip")


def derive_expectations(workbook: TableauWorkbook, workbook_file: str) -> WorkbookExpectations:
    calcs = {
        strip_brackets(calc.name): calc for ds in workbook.datasources for calc in ds.calculated_fields
    }
    tooltip_sheets = {name for ws in workbook.worksheets for name in ws.tooltip_sheets}
    sheets = [_worksheet(ws, calcs, ws.name in tooltip_sheets) for ws in workbook.worksheets]
    for dashboard in workbook.dashboards:
        contains = list(dict.fromkeys(z.worksheet for z in dashboard.zones if z.kind == "worksheet" and z.worksheet))
        slicers = []
        for zone in dashboard.zones:
            spec = field_spec(zone.field, calcs) if zone.kind == "filter" and zone.field else None
            if spec is not None and spec.qualifier is None and spec not in slicers:
                slicers.append(spec)  # filter cards on measures aren't slicers
        sheets.append(SheetExpectation(name=dashboard.name, kind="dashboard", contains=contains, slicers=slicers))
    return WorkbookExpectations(workbook=workbook_file, sheets=sheets)


def _worksheet(ws: TableauWorksheet, calcs: dict[str, TableauCalculatedField], is_tooltip: bool) -> SheetExpectation:
    refs = [*ws.rows, *ws.cols, *ws.measure_names]
    kinds = (*_ENCODINGS, "lod") if ws.is_map else _ENCODINGS
    refs += [e.field for e in ws.encodings if e.kind in kinds]
    fields: list[FieldSpec] = []
    for ref in refs:
        spec = field_spec(ref, calcs)
        if spec is not None and spec not in fields:
            fields.append(spec)

    sort = None
    for s in ws.sorts:
        by = field_spec(s.by, calcs) if s.by is not None else field_spec(s.dimension, calcs)
        if by is not None:
            sort = (by, "ascending" if s.direction == "asc" else "descending")
            break

    return SheetExpectation(
        name=ws.name,
        kind="tooltip" if is_tooltip else "worksheet",
        title=ws.title or ws.name,
        fields=fields,
        sort=sort,
    )


def field_spec(ref: TableauFieldRef, calcs: dict[str, TableauCalculatedField]) -> FieldSpec | None:
    """The field spec a Tableau column instance should become in Power BI."""

    name, agg = ref.field, ref.aggregation
    table = _TABLE_COUNT.match(name)
    if table and agg in ("cnt", "cntd"):
        return FieldSpec(table.group(1), "rowcount")
    calc = calcs.get(name)
    if calc is not None:
        if agg == "usr" or calc.role is TableauRole.MEASURE:
            return FieldSpec(calc.display_name, "measure")
        return FieldSpec(calc.display_name)  # a dimension calc: a column, e.g. a colour split
    if agg in _DATE_LEVELS:
        return FieldSpec(name, _DATE_LEVELS[agg])
    if agg in _AGGREGATIONS:
        return FieldSpec(name, _AGGREGATIONS[agg])
    return FieldSpec(name)
