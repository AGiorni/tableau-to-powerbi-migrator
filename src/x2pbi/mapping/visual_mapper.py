"""Map source views to Power BI report pages and visuals.

Each view becomes a page with a single visual. Field references from the view's shelves and
colour encoding are resolved against the migrated model (columns vs measures) and placed into
the appropriate field wells for the chosen visual type.

Source-agnostic: it reads only the normalized ``MarkType``/``Aggregation`` enums and the shelf
convention (cols = X, rows = Y) that every adapter fills in, never a tool's own vocabulary. A
view whose mark has no deterministic rule is handed to the visual router along with the source's
own mark name, so an unmapped mark degrades to an LLM choice rather than a skipped page.
"""

from __future__ import annotations

import uuid

from x2pbi.ir.powerbi_ir import (
    FieldKind,
    PBIFieldRef,
    PBIModel,
    PBIReportPage,
    PBIVisual,
)
from x2pbi.ir.source_ir import (
    DASHBOARD_GRID,
    Aggregation,
    MarkType,
    SourceDashboardZone,
    SourceFieldRef,
    SourceView,
    SourceWorkbook,
)
from x2pbi.mapping.report import IssueSeverity, MigrationReport
from x2pbi.mapping.visual_catalog import enforce_wells
from x2pbi.mapping.visual_decision import (
    Ambiguity,
    DefaultRouter,
    MappingDecision,
    VisualRouter,
)

# Aggregation -> Power BI QueryAggregateFunction enum.
# 0=Sum,1=Average,2=DistinctCount,3=Min,4=Max,5=Count,6=Median (per official PBIR docs).
# Aggregations absent here (NONE, and OTHER for things like a date truncation) leave the field
# as a plain column, which is what Power BI wants for a grouping.
_AGG_FUNCTION: dict[Aggregation, int] = {
    Aggregation.SUM: 0,
    Aggregation.AVG: 1,
    Aggregation.COUNT_DISTINCT: 2,
    Aggregation.MIN: 3,
    Aggregation.MAX: 4,
    Aggregation.COUNT: 5,
    Aggregation.MEDIAN: 6,
}

# Mark type -> Power BI visual type, by chart orientation.
_VERTICAL_TYPE: dict[MarkType, str] = {
    MarkType.BAR: "clusteredColumnChart",
    MarkType.LINE: "lineChart",
    MarkType.AREA: "areaChart",
}
_HORIZONTAL_TYPE: dict[MarkType, str] = {
    MarkType.BAR: "clusteredBarChart",
    MarkType.LINE: "lineChart",
    MarkType.AREA: "areaChart",
}
_TABLE_MARKS = {MarkType.TEXT, MarkType.SQUARE}

# Marks with a deterministic chart rule; other marks are handed to the router as unmatched.
_RECOGNIZED_MARKS = set(_VERTICAL_TYPE) | {MarkType.AUTOMATIC}

# Visual types the router may choose from for a view no deterministic rule could map.
_FALLBACK_CANDIDATES = (
    "clusteredColumnChart", "clusteredBarChart", "lineChart", "areaChart",
    "pieChart", "donutChart", "scatterChart", "tableEx", "pivotTable",
    "cardVisual", "gauge", "treemap", "funnel", "waterfallChart", "ribbonChart", "azureMap",
)

# Page canvas is 1920x1080; place one visual with a margin.
_VISUAL_BOX = {"x": 40.0, "y": 40.0, "width": 1840.0, "height": 1000.0}


def map_visuals(
    workbook: SourceWorkbook,
    model: PBIModel,
    report: MigrationReport,
    router: VisualRouter | None = None,
) -> list[PBIReportPage]:
    """Build one report page per view. Views with no mappable fields are skipped.

    Deterministic rules decide each view's mapping; ambiguous ones are handed to ``router``
    (default: keep the rule-based guess and flag it). Every mapping is then checked against the
    visual catalog so only roles the visual supports are emitted.
    """

    router = router or DefaultRouter()
    column_table = {c.name: t.name for t in model.tables for c in t.columns}
    measure_table = {m.name: t.name for t in model.tables for m in t.measures}
    calc_display = _calc_display_map(workbook)

    pages: list[PBIReportPage] = []
    by_view: dict[str, PBIVisual] = {}
    for view in workbook.views:
        decision = _map_view(view, column_table, measure_table, calc_display)
        if decision is None:
            report.add(
                IssueSeverity.WARNING,
                "visual",
                view.name,
                "View had no mappable fields (e.g. a map or multi-measure sheet); skipped.",
            )
            continue

        if decision.ambiguity is not None:
            reasons = "; ".join(decision.ambiguity.reasons)
            decision = router.resolve(decision)
            if not decision.visual_type or not decision.wells:
                report.add(
                    IssueSeverity.WARNING,
                    "visual",
                    view.name,
                    f"No visual could be determined ({reasons}); skipped. "
                    "Enable an LLM router to migrate unmatched views.",
                )
                continue
            report.add(
                IssueSeverity.INFO,
                "visual",
                view.name,
                f"Mapping ({reasons}) resolved by {decision.source.value} to "
                f"'{decision.visual_type}'. Review recommended.",
            )

        wells, warnings = enforce_wells(decision.visual_type, decision.wells)
        for warning in warnings:
            report.add(IssueSeverity.WARNING, "visual", view.name, warning)
        if not wells:
            report.add(
                IssueSeverity.WARNING,
                "visual",
                view.name,
                "No valid field wells after catalog validation; skipped.",
            )
            continue

        visual = _to_visual(decision, wells, view.name)
        by_view[view.name] = visual
        pages.append(PBIReportPage(name=_new_id(), display_name=view.name, visuals=[visual]))

    pages.extend(
        _map_dashboards(workbook, by_view, column_table, measure_table, calc_display, report)
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


# Adapters normalize dashboard zones onto a square grid; map it onto the report canvas.
_CANVAS_W = 1920.0
_CANVAS_H = 1080.0


def _map_dashboards(
    workbook: SourceWorkbook,
    by_view: dict[str, PBIVisual],
    column_table: dict[str, str],
    measure_table: dict[str, str],
    calc_display: dict[str, str],
    report: MigrationReport,
) -> list[PBIReportPage]:
    """Build one report page per dashboard: view visuals placed by zone, plus filter slicers."""

    pages: list[PBIReportPage] = []
    for dash in workbook.dashboards:
        visuals: list[PBIVisual] = []
        seen_fields: set[tuple[str, str]] = set()
        for zone in dash.zones:
            box = _zone_box(zone)
            if zone.kind == "view":
                source = by_view.get(zone.view or "")
                if source is None:
                    report.add(
                        IssueSeverity.INFO,
                        "dashboard",
                        dash.name,
                        f"View '{zone.view}' has no migrated visual; omitted from dashboard.",
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


def _zone_box(zone: SourceDashboardZone) -> dict[str, float]:
    return {
        "x": zone.x / DASHBOARD_GRID * _CANVAS_W,
        "y": zone.y / DASHBOARD_GRID * _CANVAS_H,
        "width": zone.w / DASHBOARD_GRID * _CANVAS_W,
        "height": zone.h / DASHBOARD_GRID * _CANVAS_H,
    }


def _map_view(
    view: SourceView,
    column_table: dict[str, str],
    measure_table: dict[str, str],
    calc_display: dict[str, str],
) -> MappingDecision | None:
    def resolve_all(refs) -> list[PBIFieldRef]:
        return [f for r in refs if (f := _resolve_field(r, column_table, measure_table, calc_display))]

    row_dims, row_measures = _split(resolve_all(view.rows))
    col_dims, col_measures = _split(resolve_all(view.cols))
    color_dims, _ = _split(
        [f for e in view.encodings if e.kind == "color" for f in resolve_all([e.field])]
    )
    measure_values = resolve_all(view.measure_names)
    encoding_fields = [f for e in view.encodings for f in resolve_all([e.field])]
    shelf_measures = row_measures + col_measures
    all_fields = _dedup([*row_dims, *col_dims, *shelf_measures, *encoding_fields, *measure_values])
    features = _view_features(view, row_dims, col_dims, row_measures, col_measures, color_dims)

    # A geographic view -> map (location = dimensions, measure = size).
    if view.is_map:
        return _map_geographic(all_fields) or _fallback(all_fields, features)

    # A multi-measure sheet, a text/square mark, or a view with no measures -> table.
    if view.measure_names or view.mark in _TABLE_MARKS or not shelf_measures:
        values = _dedup([*row_dims, *col_dims, *shelf_measures, *measure_values])
        if values:
            return MappingDecision(
                visual_type="tableEx",
                wells={"Values": values},
                rationale="Multi-measure table or text/no-measure mark -> table.",
            )
        return _fallback(all_fields, features)

    # An unrecognized mark (e.g. pie, circle) has no deterministic rule -> hand to the router.
    if view.mark not in _RECOGNIZED_MARKS:
        return _fallback(all_fields, features)

    # Shelf convention: cols = X axis, rows = Y axis. A measure on cols -> horizontal bars.
    horizontal = bool(col_measures) and not row_measures
    if horizontal:
        axis, values, trellis = row_dims, col_measures, col_dims
        visual_type = _HORIZONTAL_TYPE.get(view.mark, "clusteredBarChart")
    else:
        axis, values, trellis = col_dims, (row_measures or col_measures), row_dims
        visual_type = _VERTICAL_TYPE.get(view.mark, "clusteredColumnChart")

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

    # The shelf structure disambiguates this: a dimension on the measure's row shelf is a
    # small-multiples trellis (-> Rows); a colour-encoding dimension is a legend (-> Series). Both
    # are read deterministically, so this is a confident decision (not routed to the LLM).
    return MappingDecision(
        visual_type=visual_type,
        wells=wells,
        sort_field=values[0] if values else None,
        rationale="cols=X (Category), rows=Y; trellis dim -> Rows (small multiples), colour -> Series.",
    )


def _view_features(
    view: SourceView,
    row_dims: list[PBIFieldRef],
    col_dims: list[PBIFieldRef],
    row_measures: list[PBIFieldRef],
    col_measures: list[PBIFieldRef],
    color_dims: list[PBIFieldRef],
) -> dict:
    """Compact, LLM-friendly summary of a view's shelves for a router to reason over.

    ``native_mark`` carries the source tool's own word for the mark (e.g. "Pie"), which is a much
    stronger hint to the router than the normalized ``MarkType.OTHER`` it collapses to.
    """

    def names(fields: list[PBIFieldRef]) -> list[str]:
        return [f.property for f in fields]

    return {
        "mark": view.mark.value,
        "native_mark": view.native_mark,
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
        rationale="Geographic view -> azureMap (location = dimensions, measure = size).",
    )


def _fallback(all_fields: list[PBIFieldRef], features: dict) -> MappingDecision | None:
    """A view no rule could map: hand its resolved fields to the router, else skip."""

    if not all_fields:
        return None
    return MappingDecision(
        visual_type="",
        ambiguity=Ambiguity(
            reasons=["no deterministic rule matched this view"],
            candidates=list(_FALLBACK_CANDIDATES),
            available_fields=all_fields,
            features=features,
        ),
    )


def _resolve_field(
    ref: SourceFieldRef,
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


def _calc_display_map(workbook: SourceWorkbook) -> dict[str, str]:
    """Map a calculated field's internal name (e.g. 'Calculation_123') to its display name."""

    return {
        calc.name: calc.display_name
        for ds in workbook.datasources
        for calc in ds.calculated_fields
    }


def _new_id() -> str:
    return uuid.uuid4().hex[:20]
