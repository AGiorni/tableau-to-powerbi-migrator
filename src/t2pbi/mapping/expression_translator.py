"""Translate Tableau calculated-field formulas into DAX, and judge the result.

These are the building blocks of the expression loop in the migration graph
(``translate_expressions`` -> ``validate_expressions`` -> repair). They hold no loop or retry
logic of their own: the graph decides when to translate, when to retry with feedback, and when to
give up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from t2pbi.ir.powerbi_ir import PBIModel
from t2pbi.ir.tableau_ir import TableauCalculatedField
from t2pbi.llm import provider_error_message
from t2pbi.mapping.model_mapper import UNTRANSLATED_DAX
from t2pbi.validate import validate_dax

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?|\n?```$")
# Strips an accidental "MeasureName =" / "'My Measure' =" prefix. Deliberately excludes
# bracketed/quoted-value starts so it never eats a real DAX expression like "[a] = [b]".
_LEADING_ASSIGN_RE = re.compile(r"^\s*'?[A-Za-z_][A-Za-z0-9_ ]*'?\s*=\s*")

EMPTY_RESPONSE_FEEDBACK = "The previous response was empty; return a valid DAX expression."

_SYSTEM_PROMPT = (
    "You are an expert in both Tableau calculation language and Power BI DAX. "
    "Translate a single Tableau calculated field into an equivalent DAX measure expression. "
    "Rules:\n"
    "- Return ONLY the DAX expression, no explanation, no 'Measure =' prefix, no code fences.\n"
    "- ALWAYS reference columns fully qualified as 'TableName'[ColumnName] using the exact "
    "names from the provided model schema. Never use a bare column name.\n"
    "- Only reference tables and columns that appear in the schema. If a field referenced by "
    "the Tableau formula is not in the schema, assume it is a column of the field's own table.\n"
    "- Preserve aggregation semantics. Tableau row-level vs aggregate context maps to DAX "
    "iterator vs aggregation functions.\n"
    "- Translate LOD expressions ({FIXED}/{INCLUDE}/{EXCLUDE}) using CALCULATE + ALLEXCEPT/"
    "ALL/VALUES as appropriate.\n"
    "- If a construct cannot be translated, return BLANK() and nothing else."
)


@dataclass(frozen=True)
class DaxCheck:
    """Verdict on one candidate DAX expression.

    ``rejections`` make the candidate unusable and are fed back to the LLM on repair (empty
    answer, unbalanced delimiters). ``reference_warnings`` are kept as report warnings only:
    Tableau formulas often reference source columns that never appear as workbook fields.
    """

    rejections: list[str] = field(default_factory=list)
    reference_warnings: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return not self.rejections


@dataclass(frozen=True)
class Translation:
    """One translation attempt: cleaned DAX, or None with the provider's error when the call failed."""

    dax: str | None
    error: str | None = None


def translate_calc(
    calc: TableauCalculatedField,
    owning_table: str | None,
    schema_context: str,
    chat_model: Any,
    feedback: list[str] | None = None,
) -> Translation:
    """Ask the chat model for one DAX expression.

    Never raises: a provider failure comes back as ``Translation(None, error)`` so the migration
    continues, but the reason reaches the report instead of being swallowed.
    """

    from langchain_core.messages import HumanMessage, SystemMessage

    user = (
        f"Model schema (tables and their columns):\n{schema_context}\n\n"
        f"Field name: {calc.display_name}\n"
        f"This field belongs to table: {owning_table or '(unknown)'}\n"
        f"Result data type: {calc.datatype.value}\n"
        f"Is LOD: {calc.is_lod}\n"
        f"Tableau formula:\n{calc.formula}"
    )
    if feedback:
        user += "\n\nYour previous attempt was invalid: " + "; ".join(feedback) + "\nReturn corrected DAX."
    try:
        response = chat_model.invoke(
            [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=user)]
        )
    except Exception as exc:  # noqa: BLE001 - degrade gracefully on any provider error
        return Translation(None, provider_error_message(exc))
    content = getattr(response, "content", None)
    return Translation(clean_dax(content if isinstance(content, str) else None))


def check_dax(dax: str | None, model: PBIModel) -> DaxCheck:
    """Judge a candidate: reject empty/placeholder or syntactically broken DAX."""

    if not dax or dax == UNTRANSLATED_DAX:
        return DaxCheck(rejections=[EMPTY_RESPONSE_FEEDBACK])
    issues = validate_dax(dax, model)
    return DaxCheck(
        rejections=[i.message for i in issues if i.kind == "delimiter"],
        reference_warnings=[i.message for i in issues if i.kind == "reference"],
    )


def schema_context(model: PBIModel) -> str:
    """Render a compact 'Table: [col1], [col2] ...' listing for prompt grounding."""

    lines: list[str] = []
    for table in model.tables:
        cols = ", ".join(f"[{c.name}]" for c in table.columns)
        lines.append(f"'{table.name}': {cols}")
    return "\n".join(lines)


def clean_dax(raw: str | None) -> str | None:
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
