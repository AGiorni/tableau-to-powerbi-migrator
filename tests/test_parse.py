"""Tests for the Tableau adapter: Tableau XML → the neutral source IR."""

from __future__ import annotations

from x2pbi.ir.source_ir import Aggregation, ConnectionKind, DataType, FieldRole
from x2pbi.sources.tableau import load_workbook, parse_workbook


def test_parse_superstore(superstore_twb):
    workbook = parse_workbook(load_workbook(superstore_twb))

    assert workbook.source_name == "superstore"
    # The adapter stamps its own name so the translator can pick the right dialect.
    assert workbook.dialect == "tableau"
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
    assert sales.role is FieldRole.MEASURE
    assert sales.datatype is DataType.REAL
    # Tableau's "Sum" is normalized to the neutral aggregation enum.
    assert sales.aggregation is Aggregation.SUM


def test_lod_detection(superstore_twb):
    # A Tableau LOD sets the neutral "scoped aggregation" flag the translator prompts on.
    workbook = parse_workbook(load_workbook(superstore_twb))
    ds = workbook.datasources[0]

    lod = next(c for c in ds.calculated_fields if c.display_name == "Sales per Customer")
    assert lod.is_scoped_aggregation is True

    ratio = next(c for c in ds.calculated_fields if c.display_name == "Profit Ratio")
    assert ratio.is_scoped_aggregation is False


def test_connections_parsed(superstore_twb):
    workbook = parse_workbook(load_workbook(superstore_twb))
    ds = workbook.datasources[0]

    # The Tableau class is normalized to a neutral kind, with the original kept for messages.
    csv = next(c for c in ds.connections if c.kind is ConnectionKind.CSV)
    assert csv.filename == "orders.csv"
    assert csv.native_class == "textscan"
