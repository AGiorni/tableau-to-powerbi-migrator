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

# Tableau mark class -> Power BI visual type, by chart orientation.
_VERTICAL_TYPE = {"Bar": "clusteredColumnChart", "Line": "lineChart", "Area": "areaChart"}
_HORIZONTAL_TYPE = {"Bar": "clusteredBarChart", "Line": "lineChart", "Area": "areaChart"}
_TABLE_MARKS = {"Text", "Square"}

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
    def resolve_all(refs) -> list[PBIFieldRef]:
        return [f for r in refs if (f := _resolve_field(r, column_table, measure_table, calc_display))]

    row_dims, row_measures = _split(resolve_all(ws.rows))
    col_dims, col_measures = _split(resolve_all(ws.cols))
    color_dims, _ = _split(
        [f for e in ws.encodings if e.kind == "color" for f in resolve_all([e.field])]
    )
    measures = row_measures + col_measures

    # A text/table mark, or a sheet with no measures, becomes a table.
    if ws.mark_class in _TABLE_MARKS or not measures:
        values = [*row_dims, *col_dims, *row_measures, *col_measures]
        return PBIVisual(name=_new_id(), visual_type="tableEx", wells={"Values": values}, **_VISUAL_BOX) if values else None

    # Tableau convention: cols = X axis, rows = Y axis. A measure on cols -> horizontal bars.
    horizontal = bool(col_measures) and not row_measures
    if horizontal:
        axis, values, trellis = row_dims, col_measures, col_dims
        visual_type = _HORIZONTAL_TYPE.get(ws.mark_class, "clusteredBarChart")
    else:
        axis, values, trellis = col_dims, (row_measures or col_measures), row_dims
        visual_type = _VERTICAL_TYPE.get(ws.mark_class, "clusteredColumnChart")

    series = [*trellis, *color_dims]
    wells = {"Category": axis, "Y": values, "Series": series}
    wells = {name: fields for name, fields in wells.items() if fields}
    if not wells:
        return None

    return PBIVisual(
        name=_new_id(),
        visual_type=visual_type,
        wells=wells,
        sort_field=values[0] if values else None,
        **_VISUAL_BOX,
    )


def _split(fields: list[PBIFieldRef]) -> tuple[list[PBIFieldRef], list[PBIFieldRef]]:
    """Partition resolved fields into (dimensions, measures)."""

    dims = [f for f in fields if f.kind is FieldKind.COLUMN]
    measures = [f for f in fields if f.kind is not FieldKind.COLUMN]
    return dims, measures


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
