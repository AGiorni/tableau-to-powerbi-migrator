"""Tests for the offline validation tier (DAX and model structure).

The translate/repair loop that uses these validators is tested in test_graph.py.
"""

from __future__ import annotations

from t2pbi.ir.powerbi_ir import (
    Cardinality,
    PBIColumn,
    PBIDataType,
    PBIMeasure,
    PBIModel,
    PBIRelationship,
    PBITable,
)
from t2pbi.mapping.report import IssueSeverity
from t2pbi.validate import validate_dax, validate_model


def _model_with(*, columns, measures=(), relationships=()):
    table = PBITable(
        name="Sales",
        columns=[PBIColumn(name=c, data_type=PBIDataType.DOUBLE, source_column=c) for c in columns],
        measures=list(measures),
    )
    return PBIModel(name="m", tables=[table], relationships=list(relationships))


# --- DAX validation --------------------------------------------------------------------------


def test_dax_balanced_delimiters_ok():
    model = _model_with(columns=["Sales"])
    assert validate_dax("SUM('Sales'[Sales])", model) == []


def test_dax_unbalanced_paren_is_delimiter_issue():
    model = _model_with(columns=["Sales"])
    issues = validate_dax("SUM('Sales'[Sales]", model)
    assert any(i.kind == "delimiter" for i in issues)


def test_dax_unknown_reference_is_only_a_warning_kind():
    model = _model_with(columns=["Sales"])
    issues = validate_dax("SUM('Sales'[Profit])", model)
    assert [i.kind for i in issues] == ["reference"]


def test_dax_known_measure_reference_ok():
    model = _model_with(
        columns=["Sales"],
        measures=[PBIMeasure(name="Total", expression="SUM('Sales'[Sales])")],
    )
    assert validate_dax("[Total] * 2", model) == []


# --- Model structural validation -------------------------------------------------------------


def test_model_flags_missing_relationship_column():
    model = _model_with(
        columns=["Sales"],
        relationships=[
            PBIRelationship(
                name="r1",
                from_table="Sales",
                from_column="CustomerID",  # not a column
                to_table="Sales",
                to_column="Sales",
                cardinality=Cardinality.MANY_TO_ONE,
            )
        ],
    )
    findings = validate_model(model)
    assert any("CustomerID" in f.message and f.severity is IssueSeverity.ERROR for f in findings)


def test_model_flags_missing_partition():
    model = _model_with(columns=["Sales"])  # table has no partition
    findings = validate_model(model)
    assert any("partition" in f.message for f in findings)


def test_real_sql_sample_model_is_valid(sales_sql_twb, mapped):
    # The mapper adds the hidden join-key column, so the model must validate cleanly.
    model = mapped(sales_sql_twb).model
    errors = [f for f in validate_model(model) if f.severity is IssueSeverity.ERROR]
    assert errors == []

    customers = next(t for t in model.tables if t.name == "Customers")
    key = next(c for c in customers.columns if c.name == "CustomerID")
    assert key.is_hidden is True

