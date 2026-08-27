"""Map Tableau worksheets to Power BI report pages and visuals.

Each worksheet becomes a page with a single visual. Field references from the worksheet's shelves
and colour encoding are resolved against the migrated model (columns vs measures) and placed into
the appropriate field wells for the chosen visual type.
"""

from __future__ import annotations

import uuid

from t2pbi.ir.powerbi_ir import (
    FieldKind,
    PBIFieldRef,
    PBIModel,
    PBIReportPage,
    PBIVisual,
)
from t2pbi.ir.tableau_ir import TableauFieldRef, TableauWorkbook, TableauWorksheet
from t2pbi.mapping.report import IssueSeverity, MigrationReport

# Tableau aggregation code -> Power BI aggregation Function enum.
_AGG_FUNCTION = {"sum": 0, "avg": 1, "min": 2, "max": 3, "cnt": 4, "count": 4, "cntd": 5}

# Tableau mark class -> Power BI visual type.
_MARK_TO_VISUAL = {
    "Bar": "clusteredColumnChart",
    "Line": "lineChart",
    "Area": "areaChart",
    "Pie": "pieChart",
    "Square": "tableEx",
    "Text": "tableEx",
    "Gantt Bar": "clusteredBarChart",
}

# Page canvas is 1920x1080; place one visual with a margin.
_VISUAL_BOX = {"x": 40.0, "y": 40.0, "width": 1840.0, "height": 1000.0}


def map_visuals(
    workbook: TableauWorkbook, model: PBIModel, report: MigrationReport
) -> list[PBIReportPage]:
    """Build one report page per worksheet. Worksheets with no mappable fields are skipped."""

    column_table = {c.name: t.name for t in model.tables for c in t.columns}
    measure_table = {m.name: t.name for t in model.tables for m in t.measures}
    calc_display = _calc_display_map(workbook)

    pages: list[PBIReportPage] = []
    for ws in workbook.worksheets:
        visual = _map_worksheet(ws, column_table, measure_table, calc_display, report)
        if visual is None:
            report.add(
                IssueSeverity.WARNING,
                "visual",
                ws.name,
                "Worksheet had no mappable fields (e.g. a map or Measure Names sheet); skipped.",
            )
            continue
        pages.append(
            PBIReportPage(name=_new_id(), display_name=ws.name, visuals=[visual])
        )
    return pages


def _map_worksheet(
    ws: TableauWorksheet,
    column_table: dict[str, str],
    measure_table: dict[str, str],
    calc_display: dict[str, str],
    report: MigrationReport,
) -> PBIVisual | None:
    def resolve(ref: TableauFieldRef) -> PBIFieldRef | None:
        return _resolve_field(ref, column_table, measure_table, calc_display)

    shelf_fields = [f for ref in (*ws.rows, *ws.cols) if (f := resolve(ref)) is not None]
    if not shelf_fields:
        return None

    dimensions = [f for f in shelf_fields if f.kind is FieldKind.COLUMN]
    measures = [f for f in shelf_fields if f.kind is not FieldKind.COLUMN]
    series = [
        f
        for enc in ws.encodings
        if enc.kind == "color" and (f := resolve(enc.field)) is not None and f.kind is FieldKind.COLUMN
    ]

    visual_type = _visual_type(ws.mark_class, dimensions, measures)
    if visual_type == "tableEx":
        wells = {"Values": [*dimensions, *measures]}
    else:
        wells = {"Category": dimensions, "Y": measures}
        if series:
            wells["Series"] = series
    wells = {name: fields for name, fields in wells.items() if fields}

    if not any(wells.values()):
        return None

    return PBIVisual(
        name=_new_id(),
        visual_type=visual_type,
        tab_order=0,
        wells=wells,
        **_VISUAL_BOX,
    )


def _resolve_field(
    ref: TableauFieldRef,
    column_table: dict[str, str],
    measure_table: dict[str, str],
    calc_display: dict[str, str],
) -> PBIFieldRef | None:
    name = ref.field
    display = calc_display.get(name, name)

    if display in measure_table:
        return PBIFieldRef(entity=measure_table[display], property=display, kind=FieldKind.MEASURE)

    if name in column_table:
        func = _AGG_FUNCTION.get(ref.aggregation)
        if func is not None:
            return PBIFieldRef(
                entity=column_table[name],
                property=name,
                kind=FieldKind.AGGREGATION,
                function=func,
            )
        return PBIFieldRef(entity=column_table[name], property=name, kind=FieldKind.COLUMN)

    return None


def _visual_type(mark_class: str, dimensions: list, measures: list) -> str:
    mapped = _MARK_TO_VISUAL.get(mark_class)
    if mapped:
        return mapped
    # 'Automatic' or unknown mark: infer from the field composition.
    if dimensions and measures:
        return "clusteredColumnChart"
    return "tableEx"


def _calc_display_map(workbook: TableauWorkbook) -> dict[str, str]:
    """Map a calculated field's internal name (e.g. 'Calculation_123') to its display name."""

    mapping: dict[str, str] = {}
    for ds in workbook.datasources:
        for calc in ds.calculated_fields:
            internal = calc.name[1:-1] if calc.name.startswith("[") else calc.name
            mapping[internal] = calc.display_name
    return mapping


def _new_id() -> str:
    return uuid.uuid4().hex[:20]
