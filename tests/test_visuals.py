"""Tests for visual mapping (worksheet -> page/visual) and PBIR visual JSON emission."""

from __future__ import annotations

import pytest

from t2pbi.emit.pbir_writer import build_visual_json
from t2pbi.ingest import load_workbook
from t2pbi.ir.powerbi_ir import FieldKind, PBIFieldRef, PBIVisual
from t2pbi.ir.tableau_ir import TableauFieldRef, TableauRole, TableauWorksheet
from t2pbi.mapping.model_mapper import map_model
from t2pbi.mapping.report import MigrationReport
from t2pbi.mapping.visual_mapper import map_visuals
from t2pbi.parse import parse_workbook


@pytest.fixture
def book1(book1_twb):
    """Book1 parsed, with its report pages keyed by display name."""

    workbook = parse_workbook(load_workbook(book1_twb))
    model = map_model(workbook, MigrationReport()).model
    pages = map_visuals(workbook, model, MigrationReport())
    return workbook, {p.display_name: p for p in pages}


def test_visual_json_shapes_match_reference():
    visual = PBIVisual(
        name="v1",
        visual_type="clusteredColumnChart",
        wells={
            "Category": [PBIFieldRef(entity="Orders", property="Category")],
            "Y": [PBIFieldRef(entity="Orders", property="Sales", kind=FieldKind.AGGREGATION, function=0)],
        },
    )
    j = build_visual_json(visual)

    assert j["visual"]["visualType"] == "clusteredColumnChart"
    cat = j["visual"]["query"]["queryState"]["Category"]["projections"][0]
    assert cat["field"]["Column"]["Property"] == "Category"
    assert cat["queryRef"] == "Orders.Category"
    assert cat["active"] is True

    y = j["visual"]["query"]["queryState"]["Y"]["projections"][0]
    assert y["field"]["Aggregation"]["Function"] == 0
    assert y["queryRef"] == "Sum(Orders.Sales)"
    assert y["nativeQueryRef"] == "Sum of Sales"


def test_visual_title_emitted_from_name():
    visual = PBIVisual(
        name="v1",
        visual_type="clusteredColumnChart",
        title="Sales by Category",
        wells={"Category": [PBIFieldRef(entity="Orders", property="Category")]},
    )
    title = build_visual_json(visual)["visual"]["visualContainerObjects"]["title"][0]
    assert title["properties"]["text"]["expr"]["Literal"]["Value"] == "'Sales by Category'"


def test_measure_field_expr():
    visual = PBIVisual(
        name="v",
        visual_type="clusteredColumnChart",
        wells={"Y": [PBIFieldRef(entity="Orders", property="Profit Ratio", kind=FieldKind.MEASURE)]},
    )
    y = build_visual_json(visual)["visual"]["query"]["queryState"]["Y"]["projections"][0]
    assert y["field"]["Measure"]["Property"] == "Profit Ratio"
    assert y["queryRef"] == "Orders.Profit Ratio"


def test_map_visuals_book1(book1):
    _, by_name = book1

    # Each page has one visual; at least the two bar/area sheets mapped.
    assert "Sales by Category" in by_name

    sales_cat = by_name["Sales by Category"].visuals[0]
    assert sales_cat.visual_type == "stackedAreaChart"  # Tableau stacks area marks by default
    # cols=Order Date -> X axis (Category well); rows Sales -> Y; Category dim -> Rows (small multiples).
    assert {f.property for f in sales_cat.wells.get("Category", [])} == {"Order Date"}
    assert any(
        f.property == "Sales" and f.kind is FieldKind.AGGREGATION
        for f in sales_cat.wells.get("Y", [])
    )
    assert {f.property for f in sales_cat.wells.get("Rows", [])} == {"Category"}

    # Profit-ratio-by-city: measure on cols -> horizontal bar, City on the axis.
    city = by_name["Tooltip: Profit Ratio by City"].visuals[0]
    assert city.visual_type == "clusteredBarChart"
    assert {f.property for f in city.wells.get("Category", [])} == {"City"}


def test_dashboard_page_with_slicers_book1(book1):
    workbook, by_name = book1
    # Dashboard zones are parsed: worksheets + filter cards.
    dash = workbook.dashboards[0]
    assert dash.name == "Executive Overview"
    assert any(z.kind == "worksheet" for z in dash.zones)
    assert any(z.kind == "filter" for z in dash.zones)

    assert "Executive Overview" in by_name

    exec_page = by_name["Executive Overview"]
    slicers = [v for v in exec_page.visuals if v.visual_type == "slicer"]
    # Column filters become slicers; the profit-ratio measure filter does not.
    assert {v.title for v in slicers} == {"Country/Region", "Order Date"}
    for slicer in slicers:
        assert set(slicer.wells) == {"Values"}

    # Embedded worksheet visuals are repositioned to their zone, not the full-page box.
    charts = [v for v in exec_page.visuals if v.visual_type != "slicer"]
    assert charts and any(v.x > 100 for v in charts)


def test_kpi_measure_values_table_book1(book1):
    workbook, by_name = book1
    kpi_ws = next(w for w in workbook.worksheets if w.name == "Key Performance Indicators")
    assert kpi_ws.measure_names  # measures captured from the Measure Names filter

    assert "Key Performance Indicators" in by_name

    kpi = by_name["Key Performance Indicators"].visuals[0]
    assert kpi.visual_type == "tableEx"
    props = {f.property for f in kpi.wells["Values"]}
    assert {"Discount", "Profit", "Sales", "profit ratio"} <= props


def test_map_geographic_worksheet_book1(book1):
    workbook, by_name = book1
    geo = next(w for w in workbook.worksheets if w.name == "profit ratio by geography")
    assert geo.is_map

    assert "profit ratio by geography" in by_name

    geo_visual = by_name["profit ratio by geography"].visuals[0]
    assert geo_visual.visual_type == "azureMap"
    assert {f.property for f in geo_visual.wells["Category"]} >= {"State/Province"}
    assert geo_visual.wells.get("Size")  # a measure sizes the map




# ------------------------------------------------------------------------------------------------
# Rules read from the worksheet (moved from what the refinement agent fixed). Each is stated in
# Tableau's terms, so the synthetic sheets below exercise it apart from Book1.
# ------------------------------------------------------------------------------------------------


def _sheet(**fields) -> TableauWorksheet:
    return TableauWorksheet(name=fields.pop("name", "S"), datasource_names=["ds"], **fields)


def _ref(text: str) -> TableauFieldRef:
    agg, name, role = text.split(":")
    return TableauFieldRef(field=name, aggregation=agg, role=TableauRole.MEASURE if role == "qk" else TableauRole.DIMENSION)


def _orders_model():
    from t2pbi.ir.powerbi_ir import PBIColumn, PBIModel, PBITable

    columns = [PBIColumn(name=n, data_type="string", source_column=n) for n in ("Region", "Segment", "Order Date")]
    columns += [PBIColumn(name=n, data_type="double", source_column=n) for n in ("Sales", "Profit")]
    return PBIModel(name="M", tables=[PBITable(name="Orders", columns=columns)])


def _map(*sheets, dashboards=()):
    from t2pbi.ir.tableau_ir import TableauWorkbook

    workbook = TableauWorkbook(source_name="W", worksheets=list(sheets), dashboards=list(dashboards))
    report = MigrationReport()
    pages = map_visuals(workbook, _orders_model(), report)
    return {p.display_name: p for p in pages}, report


def _roles(visual) -> dict[str, list[str]]:
    return {role: [f.property for f in fields] for role, fields in visual.wells.items()}


def test_bars_split_by_a_colour_dimension_stack_as_tableau_does():
    from t2pbi.ir.tableau_ir import TableauEncoding

    split = _sheet(name="split", mark_class="Bar", cols=[_ref("none:Region:nk")], rows=[_ref("sum:Sales:qk")],
                   encodings=[TableauEncoding(kind="color", field=_ref("none:Segment:nk"))])
    plain = _sheet(name="plain", mark_class="Bar", cols=[_ref("none:Region:nk")], rows=[_ref("sum:Sales:qk")])
    sideways = split.model_copy(update={"name": "sideways", "cols": split.rows, "rows": split.cols})
    pages, _ = _map(split, plain, sideways)
    assert pages["split"].visuals[0].visual_type == "columnChart"  # stacked column
    assert pages["sideways"].visuals[0].visual_type == "barChart"  # stacked bar
    assert pages["plain"].visuals[0].visual_type == "clusteredColumnChart"  # nothing to stack


def test_the_tooltip_shelf_title_and_default_sort_come_from_the_sheet():
    from t2pbi.ir.tableau_ir import TableauEncoding

    sheet = _sheet(mark_class="Line", cols=[_ref("none:Order Date:nk")], rows=[_ref("sum:Sales:qk")],
                   title="Sales over time",
                   encodings=[TableauEncoding(kind="tooltip", field=_ref("sum:Profit:qk")),
                              TableauEncoding(kind="tooltip", field=_ref("none:Region:nk"))])
    visual = _map(sheet)[0]["S"].visuals[0]
    assert visual.title == "Sales over time"
    assert _roles(visual)["Tooltips"] == ["Profit"]  # Power BI tooltips take measures
    # No sort in Tableau: its default, the axis dimension in its own order.
    assert (visual.sort_field.property, visual.sort_descending) == ("Order Date", False)


def test_the_sheets_own_sort_wins():
    from t2pbi.ir.tableau_ir import TableauSort

    sheet = _sheet(mark_class="Bar", rows=[_ref("none:Region:nk")], cols=[_ref("sum:Sales:qk")],
                   sorts=[TableauSort(dimension=_ref("none:Region:nk"), by=_ref("sum:Sales:qk"), direction="desc")])
    visual = _map(sheet)[0]["S"].visuals[0]
    assert (visual.sort_field.property, visual.sort_descending) == ("Sales", True)


def test_a_viz_in_tooltip_sheet_is_a_tooltip_page_linked_from_every_copy_of_its_host():
    from t2pbi.ir.tableau_ir import TableauDashboard, TableauDashboardZone

    host = _sheet(name="host", mark_class="Bar", cols=[_ref("none:Region:nk")], rows=[_ref("sum:Sales:qk")],
                  tooltip_sheets=["tip"])
    tip = _sheet(name="tip", mark_class="Bar", cols=[_ref("none:Segment:nk")], rows=[_ref("sum:Profit:qk")])
    dashboard = TableauDashboard(name="dash", worksheet_names=["host"],
                                 zones=[TableauDashboardZone(kind="worksheet", worksheet="host", w=50000, h=50000)])
    pages, _ = _map(host, tip, dashboards=[dashboard])

    page = pages["tip"]
    assert (page.tooltip, page.width, page.height) == (True, 320, 240)
    assert (page.visuals[0].x, page.visuals[0].width) == (0.0, 320.0)
    assert pages["host"].visuals[0].tooltip_page == page.name
    assert pages["dash"].visuals[0].tooltip_page == page.name  # the dashboard's copy hovers too


def test_a_tableau_row_count_becomes_a_countrows_measure(book1_twb):
    workbook = parse_workbook(load_workbook(book1_twb))
    model = map_model(workbook, MigrationReport()).model
    measures = {m.name: (t.name, m.expression) for t in model.tables for m in t.measures}
    assert measures["Orders (Count)"] == ("Orders", "COUNTROWS('Orders')")
    assert measures["Returns (Count)"] == ("Returns", "COUNTROWS('Returns')")
    kpi = {p.display_name: p for p in map_visuals(workbook, model, MigrationReport())}["Key Performance Indicators"]
    assert {"Orders (Count)", "Returns (Count)"} <= {f.property for f in kpi.visuals[0].wells["Values"]}


def test_a_row_count_of_a_single_table_source_finds_its_table():
    from t2pbi.ir.powerbi_ir import PBIColumn, PBIModel, PBITable
    from t2pbi.ir.tableau_ir import TableauWorkbook
    from t2pbi.mapping.model_mapper import _add_row_count_measures

    # A one-table datasource is named after the datasource, not its logical table.
    model = PBIModel(name="M", tables=[PBITable(name="Sample - Superstore", columns=[PBIColumn(name="Sales", data_type="double", source_column="Sales")])])
    sheet = _sheet(measure_names=[_ref("cnt:Orders_" + "A" * 32 + ":qk")])
    _add_row_count_measures(TableauWorkbook(source_name="W", worksheets=[sheet]), model, MigrationReport())
    assert [(m.name, m.expression) for m in model.tables[0].measures] == [("Orders (Count)", "COUNTROWS('Sample - Superstore')")]
