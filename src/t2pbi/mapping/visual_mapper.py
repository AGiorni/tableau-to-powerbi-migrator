"""Map Tableau worksheets to Power BI report pages and visuals.

Each worksheet becomes one visual. Field references from the worksheet's shelves and colour
encoding are resolved against the migrated model (columns vs measures) and placed into the
appropriate field wells for the chosen visual type.

Pages follow what a Tableau viewer can open as a tab (:func:`_gets_own_page`): every dashboard is
a page holding its worksheets' visuals, and a worksheet gets a page of its own unless it's hidden
in Tableau and shown on a dashboard. A worksheet shown in another sheet's tooltip ("viz in
tooltip") becomes a hidden report-page tooltip, linked from every copy of its host's visual.

What each visual shows is read from the worksheet, never assumed: its custom title (the static
part), the fields on its Tooltip shelf, its sort (else the dimension's own order, Tableau's
default), Tableau's default stacking (areas, and bars split by a colour dimension), and row
counts of a logical table, bound to the ``COUNTROWS`` measure the model mapper adds.
"""

from __future__ import annotations

from t2pbi.ir.powerbi_ir import (
    FieldKind,
    PBIFieldRef,
    PBIModel,
    PBIReportPage,
    PBIVisual,
    new_object_name,
)
from t2pbi.ir.tableau_ir import (
    TableauDashboardZone,
    TableauFieldRef,
    TableauWorkbook,
    TableauWorksheet,
    counted_table,
)
from t2pbi.mapping.model_mapper import row_count_measure_name
from t2pbi.mapping.report import IssueSeverity, MigrationReport
from t2pbi.mapping.visual_catalog import VISUAL_CATALOG, enforce_wells
from t2pbi.mapping.visual_decision import (
    Ambiguity,
    DefaultRouter,
    MappingDecision,
    VisualRouter,
)

# Tableau aggregation code -> Power BI QueryAggregateFunction enum.
# 0=Sum,1=Average,2=DistinctCount,3=Min,4=Max,5=Count,6=Median (per official PBIR docs).
_AGG_FUNCTION = {"sum": 0, "avg": 1, "cntd": 2, "min": 3, "max": 4, "cnt": 5, "count": 5, "median": 6}

# Tableau mark class -> Power BI visual type, by chart orientation. Tableau stacks area marks by
# default ("Stack Marks: Automatic"), so an area chart is a stacked one; bars stack too when a
# colour dimension splits them (_STACKED_TYPE). Line marks never stack.
# Not read yet: an explicit "Stack Marks: Off" (BACKLOG; its XML form isn't confirmed).
_VERTICAL_TYPE = {"Bar": "clusteredColumnChart", "Line": "lineChart", "Area": "stackedAreaChart"}
_HORIZONTAL_TYPE = {"Bar": "clusteredBarChart", "Line": "lineChart", "Area": "stackedAreaChart"}
_STACKED_TYPE = {"clusteredColumnChart": "columnChart", "clusteredBarChart": "barChart"}

# Visual types whose "Category" is a location, not an ordered axis: no default sort.
_UNORDERED_TYPES = {"azureMap", "map", "filledMap"}

# Power BI Desktop's default size for a report-page tooltip.
_TOOLTIP_PAGE_SIZE = (320, 240)
_TABLE_MARKS = {"Text", "Square"}
# Marks Tableau draws a scatter plot with when measures sit on both Rows and Columns.
_SCATTER_MARKS = {"Automatic", "Circle", "Shape"}
# Encodings whose measures a card shows (the big numbers of a KPI sheet).
_CARD_ENCODINGS = {"text", "label"}

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
    """Build one visual per worksheet, a page for each worksheet a Tableau viewer can open, and a
    page per dashboard. Worksheets with no mappable fields are skipped.

    Deterministic rules decide each worksheet's mapping; ambiguous ones are handed to ``router``
    (default: keep the rule-based guess and flag it). Every mapping is then checked against the
    visual catalog so only roles the visual supports are emitted.
    """

    router = router or DefaultRouter()
    column_table = {c.name: t.name for t in model.tables for c in t.columns}
    measure_table = {m.name: t.name for t in model.tables for m in t.measures}
    # Shelves reference fields by internal name; the model's columns and measures carry the names
    # Tableau shows (captions). Resolve every reference through this map.
    display_names = workbook.display_names()
    tooltip_sheets = {name for ws in workbook.worksheets for name in ws.tooltip_sheets}
    dashboards_of: dict[str, list[str]] = {}
    for dash in workbook.dashboards:
        for name in dash.worksheet_names:
            dashboards_of.setdefault(name, []).append(dash.name)

    pages: list[PBIReportPage] = []
    by_worksheet: dict[str, PBIVisual] = {}
    for ws in workbook.worksheets:
        decision = _map_worksheet(ws, column_table, measure_table, display_names)
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
        missing = _missing_required(decision.visual_type, wells)
        if missing:
            # Never write a visual Power BI rejects: a chart without its axis falls back to what
            # can show the fields that did migrate.
            fallback = _without_required_roles(wells)
            report.add(
                IssueSeverity.WARNING, "visual", ws.name,
                f"'{decision.visual_type}' needs {', '.join(missing)}, which couldn't be migrated; "
                f"shown as '{fallback.visual_type}' instead.",
            )
            decision = decision.model_copy(update={"visual_type": fallback.visual_type})
            wells, warnings = enforce_wells(fallback.visual_type, fallback.wells)
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

        sort_field, descending = _sort_of(ws, wells, decision, column_table, measure_table, display_names)
        visual = _to_visual(decision, wells, ws.title or ws.name, sort_field, descending)
        visual.source_sheet = ws.name
        by_worksheet[ws.name] = visual
        if _gets_own_page(ws, ws.name in tooltip_sheets, dashboards_of.get(ws.name, []), report):
            if ws.name in tooltip_sheets:
                pages.append(_tooltip_page(ws.name, visual))
            else:
                pages.append(PBIReportPage(name=new_object_name(), display_name=ws.name, visuals=[visual]))

    _link_tooltip_pages(workbook, by_worksheet, pages, report)  # before dashboards copy the hosts
    pages.extend(
        _map_dashboards(workbook, by_worksheet, column_table, measure_table, display_names, report)
    )
    return pages


def _gets_own_page(ws: TableauWorksheet, in_a_tooltip: bool, shown_on: list[str], report: MigrationReport) -> bool:
    """Whether a worksheet becomes a page of its own: what a Tableau viewer can open as a tab.

    - visible: yes — a viewer can open it, whether or not a dashboard also shows it;
    - shown in another sheet's tooltip: yes, hidden or not (it becomes the tooltip page);
    - hidden and shown on a dashboard: no — viewers only see it there, so it's only a visual on
      that dashboard's page;
    - hidden and on no dashboard (only in a story, which isn't migrated): yes, with a warning,
      so nothing is lost.
    """

    if not ws.hidden or in_a_tooltip:
        return True
    if shown_on:
        report.add(
            IssueSeverity.INFO, "visual", ws.name,
            "Hidden in Tableau: no page of its own; it's a visual on "
            + ", ".join(f"'{d}'" for d in shown_on) + ".",
        )
        return False
    report.add(
        IssueSeverity.WARNING, "visual", ws.name,
        "Hidden in Tableau but shown on no dashboard (stories aren't migrated); "
        "kept as a page of its own so nothing is lost.",
    )
    return True


def _to_visual(
    decision: MappingDecision,
    wells: dict[str, list[PBIFieldRef]],
    title: str,
    sort_field: PBIFieldRef | None,
    sort_descending: bool,
) -> PBIVisual:
    return PBIVisual(
        name=new_object_name(),
        visual_type=decision.visual_type,
        title=title,
        wells=wells,
        sort_field=sort_field,
        sort_descending=sort_descending,
        **_VISUAL_BOX,
    )


def _sort_of(
    ws: TableauWorksheet,
    wells: dict[str, list[PBIFieldRef]],
    decision: MappingDecision,
    column_table: dict[str, str],
    measure_table: dict[str, str],
    display_names: dict[str, str],
) -> tuple[PBIFieldRef | None, bool]:
    """The visual's sort: the worksheet's own sort when it names a field the visual shows; else
    Tableau's default, the axis dimension in its own order (alphabetical, or by date); else
    whatever the router decided."""

    shown = [f for fields in wells.values() for f in fields]
    for sort in ws.sorts:
        ref = sort.by if sort.by is not None else sort.dimension
        target = _resolve_field(ref, column_table, measure_table, display_names)
        if target is not None and target in shown:
            return target, sort.direction == "desc"
    axis = wells.get("Category")
    if axis and decision.visual_type not in _UNORDERED_TYPES:
        return axis[0], False
    return decision.sort_field, decision.sort_descending


def _tooltip_page(sheet: str, visual: PBIVisual) -> PBIReportPage:
    """A viz-in-tooltip sheet as a hidden report-page tooltip, its visual filling the page."""

    width, height = _TOOLTIP_PAGE_SIZE
    fitted = visual.model_copy(update={
        "name": new_object_name(), "x": 0.0, "y": 0.0, "width": float(width), "height": float(height),
    })
    return PBIReportPage(
        name=new_object_name(), display_name=sheet, visuals=[fitted], tooltip=True, width=width, height=height,
    )


def _link_tooltip_pages(
    workbook: TableauWorkbook,
    by_worksheet: dict[str, PBIVisual],
    pages: list[PBIReportPage],
    report: MigrationReport,
) -> None:
    """Point each host visual at the tooltip page of the sheet in its tooltip. Its page shows the
    same visual object and dashboards copy it, so every copy of the host gets the hover."""

    tooltip_pages = {p.display_name: p.name for p in pages if p.tooltip}
    for ws in workbook.worksheets:
        host = by_worksheet.get(ws.name)
        tips = [t for t in ws.tooltip_sheets if t in tooltip_pages]
        if host is None or not tips:
            continue
        host.tooltip_page = tooltip_pages[tips[0]]
        if len(tips) > 1:
            report.add(
                IssueSeverity.WARNING, "visual", ws.name,
                f"Its tooltip shows several sheets ({', '.join(tips)}); Power BI shows one report-page "
                f"tooltip per visual, so only '{tips[0]}' is linked.",
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
    display_names: dict[str, str],
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
                visuals.append(source.model_copy(update={"name": new_object_name(), **box}))
            elif zone.kind == "filter" and zone.field is not None:
                field = _resolve_field(zone.field, column_table, measure_table, display_names)
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
                            name=new_object_name(), visual_type="slicer", title=field.property,
                            wells=wells, **box,
                        )
                    )
        if visuals:
            pages.append(PBIReportPage(name=new_object_name(), display_name=dash.name, visuals=visuals))
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
    display_names: dict[str, str],
) -> MappingDecision | None:
    def resolve_all(refs) -> list[PBIFieldRef]:
        return [f for r in refs if (f := _resolve_field(r, column_table, measure_table, display_names))]

    row_dims, row_measures = _split(resolve_all(ws.rows))
    col_dims, col_measures = _split(resolve_all(ws.cols))
    color_dims, _ = _split(
        [f for e in ws.encodings if e.kind == "color" for f in resolve_all([e.field])]
    )
    measure_values = resolve_all(ws.measure_names)
    encoding_fields = [f for e in ws.encodings for f in resolve_all([e.field])]
    # Tableau's Tooltip shelf -> the Tooltips role, which takes measures (and aggregations).
    _, tooltip_measures = _split([f for e in ws.encodings if e.kind == "tooltip" for f in resolve_all([e.field])])
    shelf_measures = row_measures + col_measures
    all_fields = _dedup([*row_dims, *col_dims, *shelf_measures, *encoding_fields, *measure_values])
    features = _worksheet_features(ws, row_dims, col_dims, row_measures, col_measures, color_dims)

    # A geographic worksheet -> map (location = dimensions, measure = size).
    if ws.is_map:
        return _map_geographic(all_fields, tooltip_measures) or _fallback(all_fields, features)

    # Measures and no dimension anywhere: Tableau shows totals (a KPI sheet's big numbers) -> a card.
    card_measures = _dedup([*shelf_measures, *[f for e in ws.encodings if e.kind in _CARD_ENCODINGS
                                                 for f in resolve_all([e.field]) if f.kind is not FieldKind.COLUMN]])
    has_dimension = any(f.kind is FieldKind.COLUMN for f in [*row_dims, *col_dims, *encoding_fields])
    if card_measures and not has_dimension and not ws.measure_names:
        return MappingDecision(
            visual_type="cardVisual",
            wells={"Data": card_measures, "Tooltips": [f for f in tooltip_measures if f not in card_measures]},
            rationale="Measures with no dimension -> card.",
        )

    # A pie: the Angle encoding's measure split into slices by the Colour dimension.
    if ws.mark_class == "Pie":
        angle = [f for e in ws.encodings if e.kind == "wedge-size" for f in resolve_all([e.field])]
        if color_dims and angle:
            return MappingDecision(
                visual_type="pieChart",
                wells={"Category": color_dims[:1], "Y": angle[:1],
                       "Tooltips": [f for f in tooltip_measures if f not in angle[:1]]},
                rationale="Pie mark: Colour -> slices (Category), Angle -> Y.",
            )

    # Nothing on Rows or Columns, a Size measure and dimensions on the marks: Tableau's treemap.
    size_measures = [f for e in ws.encodings if e.kind == "size" for f in resolve_all([e.field])
                     if f.kind is not FieldKind.COLUMN]
    mark_dims = _dedup([f for e in ws.encodings if e.kind in ("text", "label", "color", "lod")
                        for f in resolve_all([e.field]) if f.kind is FieldKind.COLUMN])
    if not (ws.rows or ws.cols) and size_measures and mark_dims and ws.mark_class in ("Automatic", "Square"):
        return MappingDecision(
            visual_type="treemap",
            wells={"Group": mark_dims[:1], "Details": mark_dims[1:2], "Values": size_measures[:1],
                   "Tooltips": [f for f in tooltip_measures if f not in size_measures[:1]]},
            rationale="Empty shelves, Size measure, dimensions on the marks -> treemap.",
        )

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

    # Measures on both Rows and Columns, no dimension there: Tableau's scatter plot.
    if row_measures and col_measures and not (row_dims or col_dims) and ws.mark_class in _SCATTER_MARKS:
        return _scatter(ws, col_measures, row_measures, color_dims, tooltip_measures, resolve_all)

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
    if color_dims:
        visual_type = _STACKED_TYPE.get(visual_type, visual_type)  # Tableau stacks split bars

    # Trellis dims (a dimension sharing the measure's shelf) default to small multiples ("Rows");
    # a colour-encoding dimension becomes an overlaid legend series ("Series").
    wells = {
        "Category": axis,
        "Y": values,
        "Rows": trellis,
        "Series": color_dims,
        "Tooltips": [f for f in tooltip_measures if f not in values],
    }
    wells = {name: fields for name, fields in wells.items() if fields}
    if not wells:
        return None

    # Tableau's shelf structure disambiguates this: a dimension on the measure's row shelf is a
    # small-multiples trellis (-> Rows); a colour-encoding dimension is a legend (-> Series). Both
    # are read deterministically, so this is a confident decision (not routed to the LLM).
    return MappingDecision(
        visual_type=visual_type,
        wells=wells,
        sort_field=values[0] if values else None,
        rationale="cols=X (Category), rows=Y; trellis dim -> Rows (small multiples), colour -> Series.",
    )


def _scatter(
    ws: TableauWorksheet,
    x: list[PBIFieldRef],
    y: list[PBIFieldRef],
    color_dims: list[PBIFieldRef],
    tooltip_measures: list[PBIFieldRef],
    resolve_all,
) -> MappingDecision:
    """A scatter plot: the first measure on Columns is X, on Rows is Y. A mark per value of the
    Detail dimension (Category), split by Colour (Series), sized by Size. Without a Detail
    dimension Tableau draws one mark, the totals, and so does Power BI."""

    detail, _ = _split([f for e in ws.encodings if e.kind == "lod" for f in resolve_all([e.field])])
    _, size = _split([f for e in ws.encodings if e.kind == "size" for f in resolve_all([e.field])])
    wells = {"X": x[:1], "Y": y[:1], "Category": detail[:1], "Series": color_dims[:1], "Size": size[:1],
             "Tooltips": [f for f in tooltip_measures if f not in (x[:1] + y[:1])]}
    return MappingDecision(
        visual_type="scatterChart",
        wells={role: fields for role, fields in wells.items() if fields},
        rationale="Measures on both Rows and Columns -> scatter (Detail -> Category, Colour -> Series).",
    )


def _missing_required(visual_type: str, wells: dict[str, list[PBIFieldRef]]) -> list[str]:
    spec = VISUAL_CATALOG.get(visual_type)
    return [role for role in spec.required if not wells.get(role)] if spec else []


def _without_required_roles(wells: dict[str, list[PBIFieldRef]]) -> MappingDecision:
    """What shows a visual's migrated fields when its required roles can't be filled: a card for
    measures alone, else a table of everything."""

    fields = _dedup([f for role_fields in wells.values() for f in role_fields])
    if fields and all(f.kind is not FieldKind.COLUMN for f in fields):
        return MappingDecision(visual_type="cardVisual", wells={"Data": fields})
    return MappingDecision(visual_type="tableEx", wells={"Values": fields})


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


def _map_geographic(
    all_fields: list[PBIFieldRef], tooltip_measures: list[PBIFieldRef]
) -> MappingDecision | None:
    """Build an azureMap: dimensions become the location, the first measure sizes the points,
    the Tooltip shelf's measures go in Tooltips."""

    dims, measures = _split(all_fields)
    if not dims:
        return None
    wells: dict[str, list[PBIFieldRef]] = {"Category": dims}
    if measures:
        wells["Size"] = [measures[0]]
    tips = [f for f in tooltip_measures if f not in wells.get("Size", [])]
    if tips:
        wells["Tooltips"] = tips
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
    display_names: dict[str, str],
) -> PBIFieldRef | None:
    name = ref.field
    display = display_names.get(name, name)

    counted = counted_table(ref)
    if counted is not None:  # Tableau's "<table> (Count)": the COUNTROWS measure the model mapper adds
        measure = row_count_measure_name(counted)
        if measure not in measure_table:
            return None
        return PBIFieldRef(entity=measure_table[measure], property=measure, kind=FieldKind.MEASURE)

    if display in measure_table:
        return PBIFieldRef(entity=measure_table[display], property=display, kind=FieldKind.MEASURE)

    column = display if display in column_table else name
    if column in column_table:
        func = _AGG_FUNCTION.get(ref.aggregation)
        if func is not None:
            return PBIFieldRef(
                entity=column_table[column],
                property=column,
                kind=FieldKind.AGGREGATION,
                function=func,
            )
        return PBIFieldRef(entity=column_table[column], property=column, kind=FieldKind.COLUMN)

    return None
