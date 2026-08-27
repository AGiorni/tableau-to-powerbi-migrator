"""Tests for ingest + parse: Tableau XML → Tableau IR."""

from __future__ import annotations

from t2pbi.ingest import load_workbook
from t2pbi.ir.tableau_ir import TableauDataType, TableauRole
from t2pbi.parse import parse_workbook


def test_parse_superstore(superstore_twb):
    workbook = parse_workbook(load_workbook(superstore_twb))

    assert workbook.source_name == "superstore"
    assert len(workbook.datasources) == 1

    ds = workbook.datasources[0]
    assert ds.display_name == "Superstore"

    # The Parameters datasource is separated out.
    assert len(workbook.parameters) == 1
    assert workbook.parameters[0].display_name == "Target Margin"

    # Physical columns (4) are separated from calculated fields (2).
    assert {c.display_name for c in ds.columns} == {
        "Sales",
        "Quantity",
        "Category",
        "Order Date",
    }
    assert {c.display_name for c in ds.calculated_fields} == {
        "Profit Ratio",
        "Sales per Customer",
    }

    sales = next(c for c in ds.columns if c.display_name == "Sales")
    assert sales.role is TableauRole.MEASURE
    assert sales.datatype is TableauDataType.REAL
    assert sales.aggregation == "Sum"


def test_lod_detection(superstore_twb):
    workbook = parse_workbook(load_workbook(superstore_twb))
    ds = workbook.datasources[0]

    lod = next(c for c in ds.calculated_fields if c.display_name == "Sales per Customer")
    assert lod.is_lod is True

    ratio = next(c for c in ds.calculated_fields if c.display_name == "Profit Ratio")
    assert ratio.is_lod is False


def test_connections_parsed(superstore_twb):
    workbook = parse_workbook(load_workbook(superstore_twb))
    ds = workbook.datasources[0]

    assert any(c.cls == "textscan" and c.filename == "orders.csv" for c in ds.connections)
