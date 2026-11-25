"""Regression tests against the real-world Book1 workbook (extract-based, logical model)."""

from __future__ import annotations

import pytest

from t2pbi.mapping.report import IssueSeverity
from t2pbi.validate import validate_model


@pytest.fixture
def model(book1_twb, mapped):
    return mapped(book1_twb).model


def test_extract_logical_tables_split(model):
    # The extract's shadow '[Extract].[...]' copies must be ignored; only logical tables remain.
    names = {t.name for t in model.tables}
    assert names == {"Orders", "Returns"}


def test_full_columns_from_metadata(model):
    orders = next(t for t in model.tables if t.name == "Orders")
    # <column> alone would yield a handful; metadata-records expose the full ~21 columns.
    assert len(orders.columns) >= 20
    names = {c.name for c in orders.columns}
    assert {"Sales", "Profit", "Order ID", "Customer Name"} <= names


def test_logical_relationship_emitted(model):
    assert len(model.relationships) == 1
    rel = model.relationships[0]
    assert rel.from_table == "Orders"
    assert rel.to_table == "Returns"
    assert rel.from_column == "Order ID"


def test_excel_partition_generated(model):
    orders = next(t for t in model.tables if t.name == "Orders")
    assert orders.partition is not None
    m = orders.partition.m_source
    assert "Excel.Workbook" in m
    # Sales must be typed as a number, not left as text (regression).
    assert '{"Sales", type number}' in m


def test_model_is_structurally_valid(model):
    errors = [f for f in validate_model(model) if f.severity is IssueSeverity.ERROR]
    assert errors == []
