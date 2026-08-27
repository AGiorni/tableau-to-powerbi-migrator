"""Regression tests against the real-world Book1 workbook (extract-based, logical model).

Skipped automatically if the sample is not present, so the suite stays portable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from t2pbi.ingest import load_workbook
from t2pbi.mapping.model_mapper import map_model
from t2pbi.parse import parse_workbook
from t2pbi.validate import validate_model

_BOOK1 = Path(__file__).resolve().parents[1] / "samples" / "input" / "Book1.twb"

pytestmark = pytest.mark.skipif(not _BOOK1.exists(), reason="Book1.twb sample not present")


def _model():
    return map_model(parse_workbook(load_workbook(_BOOK1))).model


def test_extract_logical_tables_split():
    # The extract's shadow '[Extract].[...]' copies must be ignored; only logical tables remain.
    names = {t.name for t in _model().tables}
    assert names == {"Orders", "Returns"}


def test_full_columns_from_metadata():
    orders = next(t for t in _model().tables if t.name == "Orders")
    # <column> alone would yield a handful; metadata-records expose the full ~21 columns.
    assert len(orders.columns) >= 20
    names = {c.name for c in orders.columns}
    assert {"Sales", "Profit", "Order ID", "Customer Name"} <= names


def test_logical_relationship_emitted():
    model = _model()
    assert len(model.relationships) == 1
    rel = model.relationships[0]
    assert rel.from_table == "Orders"
    assert rel.to_table == "Returns"
    assert rel.from_column == "Order ID"


def test_excel_partition_generated():
    orders = next(t for t in _model().tables if t.name == "Orders")
    assert orders.partition is not None
    assert "Excel.Workbook" in orders.partition.m_source


def test_model_is_structurally_valid():
    errors = [f for f in validate_model(_model()) if f.severity == "error"]
    assert errors == []
