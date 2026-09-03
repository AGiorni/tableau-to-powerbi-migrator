"""Tests for the offline validation tier and the translator repair loop."""

from __future__ import annotations

from x2pbi.ir.powerbi_ir import (
    Cardinality,
    PBIColumn,
    PBIDataType,
    PBIMeasure,
    PBIModel,
    PBIRelationship,
    PBITable,
)
from x2pbi.mapping.expression_translator import translate_expressions
from x2pbi.mapping.model_mapper import UNTRANSLATED_DAX, map_model
from x2pbi.mapping.report import IssueSeverity, MigrationReport
from x2pbi.sources.tableau import load_workbook, parse_workbook
from x2pbi.validate import validate_dax, validate_model


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
    assert any("CustomerID" in f.message and f.severity == "error" for f in findings)


def test_model_flags_missing_partition():
    model = _model_with(columns=["Sales"])  # table has no partition
    findings = validate_model(model)
    assert any("partition" in f.message for f in findings)


def test_real_sql_sample_model_is_valid(sales_sql_twb):
    # The mapper adds the hidden join-key column, so the model must validate cleanly.
    model = map_model(parse_workbook(load_workbook(sales_sql_twb))).model
    errors = [f for f in validate_model(model) if f.severity == "error"]
    assert errors == []

    customers = next(t for t in model.tables if t.name == "Customers")
    key = next(c for c in customers.columns if c.name == "CustomerID")
    assert key.is_hidden is True


# --- Translator repair loop ------------------------------------------------------------------


class _ScriptedChat:
    """Returns queued responses in order (one per invoke)."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self.calls = 0

    def invoke(self, _messages):
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1

        class _R:
            content = self._responses[idx]

        return _R()


def test_repair_fixes_unbalanced_dax(superstore_twb):
    result = map_model(parse_workbook(load_workbook(superstore_twb)))
    model, pending = result.model, result.pending_expressions
    report = MigrationReport()

    # First a broken (unbalanced) expression, then a valid one on retry.
    chat = _ScriptedChat(["SUM('Superstore'[Sales]", "SUM('Superstore'[Sales])"])
    translate_expressions(model, pending, chat, report)

    # It retried at least once and ended with valid DAX for the first pending field.
    assert chat.calls >= 2
    assert any(
        m.expression == "SUM('Superstore'[Sales])" for m in model.tables[0].measures
    )


def test_persistently_broken_dax_flagged_and_left_blank(superstore_twb):
    result = map_model(parse_workbook(load_workbook(superstore_twb)))
    model, pending = result.model, result.pending_expressions
    report = MigrationReport()

    chat = _ScriptedChat(["SUM('Superstore'[Sales]"])  # always unbalanced
    translate_expressions(model, pending, chat, report)

    for measure in model.tables[0].measures:
        assert measure.expression == UNTRANSLATED_DAX
    assert any(i.severity is IssueSeverity.ERROR for i in report.issues)
