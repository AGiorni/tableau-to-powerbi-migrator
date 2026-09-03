"""Tests for visual mapping (view -> page/visual) and PBIR visual JSON emission."""

from __future__ import annotations

from pathlib import Path

from x2pbi.emit.pbir_writer import build_visual_json
from x2pbi.ir.powerbi_ir import FieldKind, PBIFieldRef, PBIVisual
from x2pbi.mapping.model_mapper import map_model
from x2pbi.mapping.report import MigrationReport
from x2pbi.mapping.visual_mapper import map_visuals
from x2pbi.sources.tableau import load_workbook, parse_workbook

_BOOK1 = Path(__file__).resolve().parents[1] / "samples" / "input" / "Book1.twb"


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


def test_map_visuals_book1_if_present():
    if not _BOOK1.exists():
        return

    workbook = parse_workbook(load_workbook(_BOOK1))
    model = map_model(workbook).model
    pages = map_visuals(workbook, model, MigrationReport())

    # Each page has one visual; at least the two bar/area sheets mapped.
    assert pages
    by_name = {p.display_name: p for p in pages}
    assert "Sales by Category" in by_name

    sales_cat = by_name["Sales by Category"].visuals[0]
    assert sales_cat.visual_type == "areaChart"
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


def test_dashboard_page_with_slicers_book1_if_present():
    if not _BOOK1.exists():
        return

    workbook = parse_workbook(load_workbook(_BOOK1))
    # Dashboard zones are parsed: views + filter cards.
    dash = workbook.dashboards[0]
    assert dash.name == "Executive Overview"
    assert any(z.kind == "view" for z in dash.zones)
    assert any(z.kind == "filter" for z in dash.zones)

    model = map_model(workbook).model
    pages = map_visuals(workbook, model, MigrationReport())
    by_name = {p.display_name: p for p in pages}
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


def test_kpi_measure_values_table_book1_if_present():
    if not _BOOK1.exists():
        return

    workbook = parse_workbook(load_workbook(_BOOK1))
    kpi_view = next(v for v in workbook.views if v.name == "Key Performance Indicators")
    assert kpi_view.measure_names  # measures captured from the Measure Names filter

    model = map_model(workbook).model
    pages = map_visuals(workbook, model, MigrationReport())
    by_name = {p.display_name: p for p in pages}
    assert "Key Performance Indicators" in by_name

    kpi = by_name["Key Performance Indicators"].visuals[0]
    assert kpi.visual_type == "tableEx"
    props = {f.property for f in kpi.wells["Values"]}
    assert {"Discount", "Profit", "Sales", "profit ratio"} <= props


def test_map_worksheet_book1_if_present():
    if not _BOOK1.exists():
        return

    workbook = parse_workbook(load_workbook(_BOOK1))
    geo = next(v for v in workbook.views if v.name == "profit ratio by geography")
    assert geo.is_map

    model = map_model(workbook).model
    pages = map_visuals(workbook, model, MigrationReport())
    by_name = {p.display_name: p for p in pages}
    assert "profit ratio by geography" in by_name

    geo_visual = by_name["profit ratio by geography"].visuals[0]
    assert geo_visual.visual_type == "azureMap"
    assert {f.property for f in geo_visual.wells["Category"]} >= {"State/Province"}
    assert geo_visual.wells.get("Size")  # a measure sizes the map


