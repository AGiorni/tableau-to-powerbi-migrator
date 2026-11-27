"""Tests for Gap 1 (Power Query M partitions) and Gap 2 (relationships / multi-table)."""

from __future__ import annotations

from t2pbi.emit.tmdl_writer import render_semantic_model
from t2pbi.ir.powerbi_ir import Cardinality

# --- Gap 1: Power Query M partitions -------------------------------------------------------


def test_csv_partition_uses_csv_document(superstore_twb, mapped):
    model = mapped(superstore_twb).model
    table = model.tables[0]

    assert table.partition is not None
    m = table.partition.m_source
    assert "Csv.Document" in m
    assert "orders.csv" in m
    assert "PromoteHeaders" in m
    # No empty-table stub remains for a supported connection.
    assert "#table({}, {})" not in m
    # Columns are typed so they don't all load as text (regression).
    assert "Table.TransformColumnTypes" in m
    assert '{"Sales", type number}' in m
    assert '{"Quantity", Int64.Type}' in m


def test_sqlserver_partitions(sales_sql_twb, mapped):
    model = mapped(sales_sql_twb).model
    orders = next(t for t in model.tables if t.name == "Orders")

    m = orders.partition.m_source
    assert 'Sql.Database("sql.contoso.com", "AdventureWorks")' in m
    assert '[Schema="dbo",Item="Orders"]' in m


# --- Gap 2: multi-table split + relationships ----------------------------------------------


def test_join_splits_into_two_tables(sales_sql_twb, mapped):
    model = mapped(sales_sql_twb).model
    names = {t.name for t in model.tables}
    assert names == {"Orders", "Customers"}


def test_columns_attributed_to_correct_table(sales_sql_twb, mapped):
    model = mapped(sales_sql_twb).model
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


def test_measures_on_primary_table(sales_sql_twb, mapped):
    model = mapped(sales_sql_twb).model
    orders = next(t for t in model.tables if t.name == "Orders")
    assert {m.name for m in orders.measures} == {"Avg Sale"}


def test_relationship_emitted(sales_sql_twb, mapped):
    model = mapped(sales_sql_twb).model
    assert len(model.relationships) == 1

    rel = model.relationships[0]
    assert rel.from_table == "Orders"
    assert rel.from_column == "CustomerID"
    assert rel.to_table == "Customers"
    assert rel.to_column == "CustomerID"
    assert rel.cardinality is Cardinality.MANY_TO_ONE


def test_relationships_tmdl_rendered(sales_sql_twb, mapped):
    model = mapped(sales_sql_twb).model
    files = render_semantic_model(model)

    assert "relationships.tmdl" in files
    rel_tmdl = files["relationships.tmdl"]
    assert "relationship" in rel_tmdl
    assert "fromColumn: Orders.CustomerID" in rel_tmdl
    assert "toColumn: Customers.CustomerID" in rel_tmdl


def test_a_readded_key_column_loads_its_source_column():
    # Tableau renames a column that's in two logical tables ("Order ID (Returns)"); when that key is
    # hidden, it's added back for the relationship, and must load the source's "Order ID", or it
    # comes through blank and Power BI refuses the many-to-one relationship on refresh.
    from t2pbi.ir.powerbi_ir import PBIColumn, PBIDataType, PBIRelationship, PBITable
    from t2pbi.mapping.model_mapper import _ensure_relationship_columns

    orders = PBITable(name="Orders", columns=[PBIColumn(name="Order ID", data_type=PBIDataType.STRING,
                                                        source_column="Order ID")])
    returns = PBITable(name="Returns", columns=[])
    relationship = PBIRelationship(name="r", from_table="Orders", from_column="Order ID",
                                   to_table="Returns", to_column="Order ID (Returns)")
    _ensure_relationship_columns([orders, returns], [relationship], {"Order ID (Returns)": "Order ID"})
    key = returns.columns[0]
    assert (key.name, key.source_column, key.is_hidden) == ("Order ID (Returns)", "Order ID", True)
