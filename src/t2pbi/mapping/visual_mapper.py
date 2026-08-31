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
from t2pbi.ir.tableau_ir import (
    TableauDashboardZone,
    TableauFieldRef,
    TableauWorkbook,
    TableauWorksheet,
)
from t2pbi.mapping.report import IssueSeverity, MigrationReport
from t2pbi.mapping.visual_catalog import enforce_wells
from t2pbi.mapping.visual_decision import (
    Ambiguity,
    DefaultRouter,
    MappingDecision,
    VisualRouter,
)

# Tableau aggregation code -> Power BI QueryAggregateFunction enum.
# 0=Sum,1=Average,2=DistinctCount,3=Min,4=Max,5=Count,6=Median (per official PBIR docs).
_AGG_FUNCTION = {"sum": 0, "avg": 1, "cntd": 2, "min": 3, "max": 4, "cnt": 5, "count": 5, "median": 6}

# Tableau mark class -> Power BI visual type, by chart orientation.
_VERTICAL_TYPE = {"Bar": "clusteredColumnChart", "Line": "lineChart", "Area": "areaChart"}
_HORIZONTAL_TYPE = {"Bar": "clusteredBarChart", "Line": "lineChart", "Area": "areaChart"}
_TABLE_MARKS = {"Text", "Square"}

# Marks with a deterministic chart rule; other marks are routed to the router as unmatched.
_RECOGNIZED_MARKS = set(_VERTICAL_TYPE) | {"Automatic"}

# Visual types the router may choose from for a worksheet no deterministic rule could map.
_FALLBACK_CANDIDATES = (
    "clusteredColumnChart", "clusteredBarChart", "lineChart", "areaChart",
    "pieChart", "donutChart", "scatterChart", "tableEx", "pivotTable",
    "cardVisual", "gauge", "treemap", "funnel", "waterfallChart", "ribbonChart", "azureMap",
)

# Page canvas is 1920x1080; place one visual with a margin.
_VISUAL_BOX = {"x": 40.0, "y": 40.0, "width": 1840.0, "height": 1000.0}


def map_visuals(
    workbook: TableauWorkbook,
    model: PBIModel,
    report: MigrationReport,
    router: VisualRouter | None = None,
) -> list[PBIReportPage]:
    """Build one report page per worksheet. Worksheets with no mappable fields are skipped.

    Deterministic rules decide each worksheet's mapping; ambiguous ones are handed to ``router``
    (default: keep the rule-based guess and flag it). Every mapping is then checked against the
    visual catalog so only roles the visual supports are emitted.
    """

    router = router or DefaultRouter()
    column_table = {c.name: t.name for t in model.tables for c in t.columns}
    measure_table = {m.name: t.name for t in model.tables for m in t.measures}
    calc_display = _calc_display_map(workbook)

    pages: list[PBIReportPage] = []
    by_worksheet: dict[str, PBIVisual] = {}
    for ws in workbook.worksheets:
        decision = _map_worksheet(ws, column_table, measure_table, calc_display)
        if decision is None:
            report.add(
                IssueSeverity.WARNING,
                "visual",
                ws.name,
                "Worksheet had no mappable fields (e.g. a map or Measure Names sheet); skipped.",
            )
            continue

        if decision.ambiguity is not None:
            reasons = "; ".join(decision.ambiguity.reasons)
            decision = router.resolve(decision)
            if not decision.visual_type or not decision.wells:
                report.add(
                    IssueSeverity.WARNING,
                    "visual",
                    ws.name,
                    f"No visual could be determined ({reasons}); skipped. "
                    "Enable an LLM router to migrate unmatched worksheets.",
                )
                continue
            report.add(
                IssueSeverity.INFO,
                "visual",
                ws.name,
                f"Mapping ({reasons}) resolved by {decision.source.value} to "
                f"'{decision.visual_type}'. Review recommended.",
            )

        wells, warnings = enforce_wells(decision.visual_type, decision.wells)
        for warning in warnings:
            report.add(IssueSeverity.WARNING, "visual", ws.name, warning)
        if not wells:
            report.add(
                IssueSeverity.WARNING,
                "visual",
                ws.name,
                "No valid field wells after catalog validation; skipped.",
            )
            continue

        visual = _to_visual(decision, wells, ws.name)
        by_worksheet[ws.name] = visual
        pages.append(PBIReportPage(name=_new_id(), display_name=ws.name, visuals=[visual]))

    pages.extend(
        _map_dashboards(workbook, by_worksheet, column_table, measure_table, calc_display, report)
    )
    return pages


def _to_visual(
    decision: MappingDecision, wells: dict[str, list[PBIFieldRef]], title: str
) -> PBIVisual:
    return PBIVisual(
        name=_new_id(),
        visual_type=decision.visual_type,
        title=title,
        wells=wells,
        sort_field=decision.sort_field,
        sort_descending=decision.sort_descending,
        **_VISUAL_BOX,
    )


# Tableau lays dashboards out on a 100000 x 100000 grid; map it onto the report canvas.
_DASH_GRID = 100000
_CANVAS_W = 1920.0
_CANVAS_H = 1080.0


def _map_dashboards(
    workbook: TableauWorkbook,
    by_worksheet: dict[str, PBIVisual],
    column_table: dict[str, str],
    measure_table: dict[str, str],
    calc_display: dict[str, str],
    report: MigrationReport,
) -> list[PBIReportPage]:
    """Build one report page per dashboard: worksheet visuals placed by zone, plus filter slicers."""

    pages: list[PBIReportPage] = []
    for dash in workbook.dashboards:
        visuals: list[PBIVisual] = []
        seen_fields: set[tuple[str, str]] = set()
        for zone in dash.zones:
            box = _zone_box(zone)
            if zone.kind == "worksheet":
                source = by_worksheet.get(zone.worksheet or "")
                if source is None:
                    report.add(
                        IssueSeverity.INFO,
                        "dashboard",
                        dash.name,
                        f"Worksheet '{zone.worksheet}' has no migrated visual; omitted from dashboard.",
                    )
                    continue
                visuals.append(source.model_copy(update={"name": _new_id(), **box}))
            elif zone.kind == "filter" and zone.field is not None:
                field = _resolve_field(zone.field, column_table, measure_table, calc_display)
                if field is None or (field.entity, field.property) in seen_fields:
                    continue
                seen_fields.add((field.entity, field.property))
                if field.kind is not FieldKind.COLUMN:
                    report.add(
                        IssueSeverity.INFO,
                        "dashboard",
                        dash.name,
                        f"Filter on measure '{field.property}' not converted to a slicer "
                        "(slicers require a column).",
                    )
                    continue
                wells, _ = enforce_wells("slicer", {"Values": [field]})
                if wells:
                    visuals.append(
                        PBIVisual(
                            name=_new_id(), visual_type="slicer", title=field.property,
                            wells=wells, **box,
                        )
                    )
        if visuals:
            pages.append(PBIReportPage(name=_new_id(), display_name=dash.name, visuals=visuals))
    return pages


def _zone_box(zone: TableauDashboardZone) -> dict[str, float]:
    return {
        "x": zone.x / _DASH_GRID * _CANVAS_W,
        "y": zone.y / _DASH_GRID * _CANVAS_H,
        "width": zone.w / _DASH_GRID * _CANVAS_W,
        "height": zone.h / _DASH_GRID * _CANVAS_H,
    }


def _map_worksheet(
    ws: TableauWorksheet,
    column_table: dict[str, str],
    measure_table: dict[str, str],
    calc_display: dict[str, str],
) -> MappingDecision | None:
    def resolve_all(refs) -> list[PBIFieldRef]:
        return [f for r in refs if (f := _resolve_field(r, column_table, measure_table, calc_display))]

    row_dims, row_measures = _split(resolve_all(ws.rows))
    col_dims, col_measures = _split(resolve_all(ws.cols))
    color_dims, _ = _split(
        [f for e in ws.encodings if e.kind == "color" for f in resolve_all([e.field])]
    )
    measure_values = resolve_all(ws.measure_names)
    encoding_fields = [f for e in ws.encodings for f in resolve_all([e.field])]
    shelf_measures = row_measures + col_measures
    all_fields = _dedup([*row_dims, *col_dims, *shelf_measures, *encoding_fields, *measure_values])
    features = _worksheet_features(ws, row_dims, col_dims, row_measures, col_measures, color_dims)

    # A geographic worksheet -> map (location = dimensions, measure = size).
    if ws.is_map:
        return _map_geographic(all_fields) or _fallback(all_fields, features)

    # A Measure Names/Measure Values sheet, a text/square mark, or a sheet with no measures -> table.
    if ws.measure_names or ws.mark_class in _TABLE_MARKS or not shelf_measures:
        values = _dedup([*row_dims, *col_dims, *shelf_measures, *measure_values])
        if values:
            return MappingDecision(
                visual_type="tableEx",
                wells={"Values": values},
                rationale="Measure Values table or text/no-measure mark -> table.",
            )
        return _fallback(all_fields, features)

    # An unrecognized mark (e.g. Pie, Circle) has no deterministic rule -> hand to the router.
    if ws.mark_class not in _RECOGNIZED_MARKS:
        return _fallback(all_fields, features)

    # Tableau convention: cols = X axis, rows = Y axis. A measure on cols -> horizontal bars.
    horizontal = bool(col_measures) and not row_measures
    if horizontal:
        axis, values, trellis = row_dims, col_measures, col_dims
        visual_type = _HORIZONTAL_TYPE.get(ws.mark_class, "clusteredBarChart")
    else:
        axis, values, trellis = col_dims, (row_measures or col_measures), row_dims
        visual_type = _VERTICAL_TYPE.get(ws.mark_class, "clusteredColumnChart")

    # Trellis dims (a dimension sharing the measure's shelf) default to small multiples ("Rows");
    # a colour-encoding dimension becomes an overlaid legend series ("Series").
    wells = {
        "Category": axis,
        "Y": values,
        "Rows": trellis,
        "Series": color_dims,
    }
    wells = {name: fields for name, fields in wells.items() if fields}
    if not wells:
        return None

    # A dimension sharing the measure's shelf could be small multiples (Rows) or a legend (Series).
    ambiguity = None
    if trellis:
        names = ", ".join(f.property for f in trellis)
        reason = (
            f"dimension(s) [{names}] share the measure's shelf: "
            "small multiples (Rows) vs legend (Series)"
        )
        ambiguity = Ambiguity(
            reasons=[reason],
            candidates=[visual_type],
            features=_worksheet_features(ws, row_dims, col_dims, row_measures, col_measures, color_dims),
        )

    return MappingDecision(
        visual_type=visual_type,
        wells=wells,
        sort_field=values[0] if values else None,
        rationale="cols=X (Category), rows=Y; trellis->Rows, colour->Series.",
        ambiguity=ambiguity,
    )


def _worksheet_features(
    ws: TableauWorksheet,
    row_dims: list[PBIFieldRef],
    col_dims: list[PBIFieldRef],
    row_measures: list[PBIFieldRef],
    col_measures: list[PBIFieldRef],
    color_dims: list[PBIFieldRef],
) -> dict:
    """Compact, LLM-friendly summary of a worksheet's shelves for a router to reason over."""

    def names(fields: list[PBIFieldRef]) -> list[str]:
        return [f.property for f in fields]

    return {
        "mark_class": ws.mark_class,
        "row_dims": names(row_dims),
        "col_dims": names(col_dims),
        "row_measures": names(row_measures),
        "col_measures": names(col_measures),
        "color_dims": names(color_dims),
    }


def _split(fields: list[PBIFieldRef]) -> tuple[list[PBIFieldRef], list[PBIFieldRef]]:
    """Partition resolved fields into (dimensions, measures)."""

    dims = [f for f in fields if f.kind is FieldKind.COLUMN]
    measures = [f for f in fields if f.kind is not FieldKind.COLUMN]
    return dims, measures


def _dedup(fields: list[PBIFieldRef]) -> list[PBIFieldRef]:
    """Drop duplicate field references, preserving order."""

    seen: set[tuple[str, str, FieldKind, int | None]] = set()
    out: list[PBIFieldRef] = []
    for f in fields:
        key = (f.entity, f.property, f.kind, f.function)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def _map_geographic(all_fields: list[PBIFieldRef]) -> MappingDecision | None:
    """Build an azureMap: dimensions become the location, the first measure sizes the points."""

    dims = [f for f in all_fields if f.kind is FieldKind.COLUMN]
    measures = [f for f in all_fields if f.kind is not FieldKind.COLUMN]
    if not dims:
        return None
    wells: dict[str, list[PBIFieldRef]] = {"Category": dims}
    if measures:
        wells["Size"] = [measures[0]]
    return MappingDecision(
        visual_type="azureMap",
        wells=wells,
        rationale="Geographic worksheet -> azureMap (location = dimensions, measure = size).",
    )


def _fallback(all_fields: list[PBIFieldRef], features: dict) -> MappingDecision | None:
    """A worksheet no rule could map: hand its resolved fields to the router, else skip."""

    if not all_fields:
        return None
    return MappingDecision(
        visual_type="",
        ambiguity=Ambiguity(
            reasons=["no deterministic rule matched this worksheet"],
            candidates=list(_FALLBACK_CANDIDATES),
            available_fields=all_fields,
            features=features,
        ),
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
