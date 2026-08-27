"""Tests for Gap 1 (Power Query M partitions) and Gap 2 (relationships / multi-table)."""

from __future__ import annotations

from t2pbi.emit.tmdl_writer import render_semantic_model
from t2pbi.ingest import load_workbook
from t2pbi.ir.powerbi_ir import Cardinality
from t2pbi.mapping.model_mapper import map_model
from t2pbi.parse import parse_workbook


def _mapped(path):
    return map_model(parse_workbook(load_workbook(path)))


# --- Gap 1: Power Query M partitions -------------------------------------------------------


def test_csv_partition_uses_csv_document(superstore_twb):
    model = _mapped(superstore_twb).model
    table = model.tables[0]

    assert table.partition is not None
    m = table.partition.m_source
    assert "Csv.Document" in m
    assert "orders.csv" in m
    assert "PromoteHeaders" in m
    # No empty-table stub remains for a supported connection.
    assert "#table({}, {})" not in m


def test_sqlserver_partitions(sales_sql_twb):
    model = _mapped(sales_sql_twb).model
    orders = next(t for t in model.tables if t.name == "Orders")

    m = orders.partition.m_source
    assert 'Sql.Database("sql.contoso.com", "AdventureWorks")' in m
    assert '[Schema="dbo",Item="Orders"]' in m


# --- Gap 2: multi-table split + relationships ----------------------------------------------


def test_join_splits_into_two_tables(sales_sql_twb):
    model = _mapped(sales_sql_twb).model
    names = {t.name for t in model.tables}
    assert names == {"Orders", "Customers"}


def test_columns_attributed_to_correct_table(sales_sql_twb):
    model = _mapped(sales_sql_twb).model
    orders = next(t for t in model.tables if t.name == "Orders")
    customers = next(t for t in model.tables if t.name == "Customers")

    orders_cols = {c.name for c in orders.columns}
    customers_cols = {c.name for c in customers.columns}
    assert "Sales" in orders_cols
    assert "Customer Name" in customers_cols
    assert "Country" in customers_cols
    assert "Country" not in orders_cols

    # Physical source column comes from the <cols> map, not the friendly caption.
    country = next(c for c in customers.columns if c.name == "Country")
    assert country.source_column == "Country"
    name_col = next(c for c in customers.columns if c.name == "Customer Name")
    assert name_col.source_column == "CustomerName"


def test_measures_on_primary_table(sales_sql_twb):
    model = _mapped(sales_sql_twb).model
    orders = next(t for t in model.tables if t.name == "Orders")
    assert {m.name for m in orders.measures} == {"Avg Sale"}


def test_relationship_emitted(sales_sql_twb):
    model = _mapped(sales_sql_twb).model
    assert len(model.relationships) == 1

    rel = model.relationships[0]
    assert rel.from_table == "Orders"
    assert rel.from_column == "CustomerID"
    assert rel.to_table == "Customers"
    assert rel.to_column == "CustomerID"
    assert rel.cardinality is Cardinality.MANY_TO_ONE


def test_relationships_tmdl_rendered(sales_sql_twb):
    model = _mapped(sales_sql_twb).model
    files = render_semantic_model(model)

    assert "relationships.tmdl" in files
    rel_tmdl = files["relationships.tmdl"]
    assert "relationship" in rel_tmdl
    assert "fromColumn: Orders.CustomerID" in rel_tmdl
    assert "toColumn: Customers.CustomerID" in rel_tmdl
