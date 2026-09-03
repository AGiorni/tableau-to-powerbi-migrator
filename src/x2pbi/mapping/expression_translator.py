"""Translate source calculated-field formulas into DAX.

When an Azure OpenAI model is available, each pending calculated field is translated with a
constrained prompt and the resulting DAX is written back onto the corresponding measure. When no
model is configured, measures keep their ``BLANK()`` placeholder and remain flagged in the
report, so the pipeline still produces an openable model.

Source-agnostic: the formula language is described by the adapter's
:class:`~x2pbi.sources.base.ExpressionDialect`, which supplies the language name and the
dialect-specific translation rules appended to the system prompt. The validate-and-repair loop,
the schema grounding and the output cleanup are shared by every source.
"""

from __future__ import annotations

import re
from typing import Any

from x2pbi.ir.powerbi_ir import PBIMeasure, PBIModel, PBITable
from x2pbi.ir.source_ir import SourceCalculatedField
from x2pbi.mapping.model_mapper import UNTRANSLATED_DAX
from x2pbi.mapping.report import IssueSeverity, MigrationReport
from x2pbi.sources.base import GENERIC_DIALECT, ExpressionDialect
from x2pbi.validate import validate_dax

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?|\n?```$")
# Strips an accidental "MeasureName =" / "'My Measure' =" prefix. Deliberately excludes
# bracketed/quoted-value starts so it never eats a real DAX expression like "[a] = [b]".
_LEADING_ASSIGN_RE = re.compile(r"^\s*'?[A-Za-z_][A-Za-z0-9_ ]*'?\s*=\s*")

# Total translation attempts per field (1 initial + repairs) when DAX fails syntax validation.
_MAX_ATTEMPTS = 2

# Rules that hold for any source dialect; the adapter appends its own via ``dialect.rules``.
_SHARED_RULES = (
    "Return ONLY the DAX expression, no explanation, no 'Measure =' prefix, no code fences.",
    (
        "ALWAYS reference columns fully qualified as 'TableName'[ColumnName] using the exact "
        "names from the provided model schema. Never use a bare column name."
    ),
    (
        "Only reference tables and columns that appear in the schema. If a field referenced by "
        "the source formula is not in the schema, assume it is a column of the field's own table."
    ),
)

_FINAL_RULE = "If a construct cannot be translated, return BLANK() and nothing else."


def _system_prompt(dialect: ExpressionDialect) -> str:
    """Build the translation system prompt for one source dialect."""

    rules = "\n".join(f"- {rule}" for rule in (*_SHARED_RULES, *dialect.rules, _FINAL_RULE))
    return (
        f"You are an expert in both {dialect.language} and Power BI DAX. "
        f"Translate a single {dialect.name} calculated field into an equivalent DAX measure "
        f"expression. Rules:\n{rules}"
    )


def translate_expressions(
    model: PBIModel,
    pending: dict[str, SourceCalculatedField],
    chat_model: Any | None,
    report: MigrationReport,
    dialect: ExpressionDialect | None = None,
) -> PBIModel:
    """Fill in DAX for pending measures. Mutates and returns ``model``.

    ``dialect`` describes the source's formula language; it defaults to a generic one so the
    function stays usable without an adapter in hand.
    """

    dialect = dialect or GENERIC_DIALECT
    if not pending:
        return model

    tables_by_name = {table.name: table for table in model.tables}
    measures_by_key = {
        f"{table.name}::{measure.name}": measure
        for table in model.tables
        for measure in table.measures
    }

    if chat_model is None:
        for calc in pending.values():
            scoped = f" Contains a {dialect.scoped_aggregation_label}."
            report.add(
                IssueSeverity.WARNING,
                "expression",
                calc.display_name,
                "Calculated field awaiting DAX translation (no LLM configured); left as BLANK()."
                + (scoped if calc.is_scoped_aggregation else ""),
                detail=calc.formula,
            )
        return model

    schema_context = _schema_context(model)

    for key, calc in pending.items():
        measure = measures_by_key.get(key)
        if measure is None:
            continue
        table_name = key.split("::", 1)[0]
        owning_table = tables_by_name.get(table_name)
        _translate_and_repair(
            calc, measure, owning_table, schema_context, model, chat_model, report, dialect
        )

    return model


def _translate_and_repair(
    calc: SourceCalculatedField,
    measure: PBIMeasure,
    owning_table: PBITable | None,
    schema_context: str,
    model: PBIModel,
    chat_model: Any,
    report: MigrationReport,
    dialect: ExpressionDialect,
) -> None:
    """Translate one calc, retrying with validator feedback when the DAX is syntactically broken."""

    feedback: list[str] = []
    for _ in range(_MAX_ATTEMPTS):
        raw = _translate_one(calc, owning_table, schema_context, chat_model, dialect, feedback)
        dax = _clean_dax(raw)
        if not dax or dax == UNTRANSLATED_DAX:
            feedback = ["The previous response was empty; return a valid DAX expression."]
            continue

        issues = validate_dax(dax, model)
        syntax_errors = [i.message for i in issues if i.kind == "delimiter"]
        if syntax_errors:
            feedback = syntax_errors
            continue

        measure.expression = dax
        measure.description = None
        report.add(
            IssueSeverity.INFO,
            "expression",
            calc.display_name,
            f"Translated {dialect.name} calc to DAX.",
            detail=f"{calc.formula}  =>  {measure.expression}",
        )
        for ref in (i.message for i in issues if i.kind == "reference"):
            report.add(
                IssueSeverity.WARNING,
                "expression",
                calc.display_name,
                f"Translated DAX references something not in the model: {ref}",
            )
        return

    report.add(
        IssueSeverity.ERROR,
        "expression",
        calc.display_name,
        "Could not produce valid DAX after retries; left as BLANK().",
        detail=f"{calc.formula} | last errors: {'; '.join(feedback)}",
    )


def _translate_one(
    calc: SourceCalculatedField,
    owning_table: PBITable | None,
    schema_context: str,
    chat_model: Any,
    dialect: ExpressionDialect,
    feedback: list[str] | None = None,
) -> str | None:
    from langchain_core.messages import HumanMessage, SystemMessage

    owning = owning_table.name if owning_table is not None else "(unknown)"
    scoped_label = dialect.scoped_aggregation_label
    user = (
        f"Model schema (tables and their columns):\n{schema_context}\n\n"
        f"Field name: {calc.display_name}\n"
        f"This field belongs to table: {owning}\n"
        f"Result data type: {calc.datatype.value}\n"
        f"Is a {scoped_label}: {calc.is_scoped_aggregation}\n"
        f"{dialect.name} formula:\n{calc.formula}"
    )
    if feedback:
        user += "\n\nYour previous attempt was invalid: " + "; ".join(feedback) + "\nReturn corrected DAX."
    try:
        response = chat_model.invoke(
            [SystemMessage(content=_system_prompt(dialect)), HumanMessage(content=user)]
        )
    except Exception:  # noqa: BLE001 - degrade gracefully on any provider error
        return None
    content = getattr(response, "content", None)
    return content if isinstance(content, str) else None


def _schema_context(model: PBIModel) -> str:
    """Render a compact 'Table: [col1], [col2] ...' listing for prompt grounding."""

    lines: list[str] = []
    for table in model.tables:
        cols = ", ".join(f"[{c.name}]" for c in table.columns)
        lines.append(f"'{table.name}': {cols}")
    return "\n".join(lines)


def _clean_dax(raw: str | None) -> str | None:
    """Normalize raw model output into a bare DAX expression.

    Removes Markdown code fences and an accidental measure-name assignment prefix that models
    sometimes emit despite instructions.
    """

    if raw is None:
        return None
    text = raw.strip()
    text = _FENCE_RE.sub("", text).strip()
    text = _LEADING_ASSIGN_RE.sub("", text).strip()
    return text or None
