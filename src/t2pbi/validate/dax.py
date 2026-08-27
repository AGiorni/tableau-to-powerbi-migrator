"""Lightweight, dependency-free DAX validation against the in-memory model.

Two classes of problem are detected:

- ``delimiter`` : unbalanced parentheses or brackets. These are genuine syntax errors and are
  worth a repair attempt.
- ``reference`` : a ``'Table'[Column]`` (or bare ``[Name]``) reference that does not resolve to
  a known column or measure. This is only a *warning*: Tableau formulas often reference
  underlying source columns that never appear as workbook fields, so we must not reject on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from t2pbi.ir.powerbi_ir import PBIModel

# Matches 'Quoted Table'[Column] or BareTable[Column].
_QUALIFIED = re.compile(r"(?:'([^']+)'|([A-Za-z_]\w*))\[([^\]]+)\]")
_BARE = re.compile(r"\[([^\]]+)\]")


@dataclass(frozen=True)
class DaxIssue:
    kind: Literal["delimiter", "reference"]
    message: str


def validate_dax(expression: str, model: PBIModel) -> list[DaxIssue]:
    """Return all issues found in ``expression`` relative to ``model``."""

    issues = _check_delimiters(expression)
    issues.extend(_check_references(expression, model))
    return issues


def _check_delimiters(expression: str) -> list[DaxIssue]:
    issues: list[DaxIssue] = []
    for open_ch, close_ch, label in (("(", ")", "parenthesis"), ("[", "]", "bracket")):
        opened = expression.count(open_ch)
        closed = expression.count(close_ch)
        if opened != closed:
            issues.append(
                DaxIssue("delimiter", f"Unbalanced {label}: {opened} '{open_ch}' vs {closed} '{close_ch}'.")
            )
    return issues


def _check_references(expression: str, model: PBIModel) -> list[DaxIssue]:
    columns_by_table = {t.name: {c.name for c in t.columns} for t in model.tables}
    all_columns = {c for cols in columns_by_table.values() for c in cols}
    measures = {m.name for t in model.tables for m in t.measures}

    issues: list[DaxIssue] = []

    for quoted_table, bare_table, column in _QUALIFIED.findall(expression):
        table = quoted_table or bare_table
        known_cols = columns_by_table.get(table)
        if known_cols is None:
            issues.append(DaxIssue("reference", f"Unknown table '{table}'."))
        elif column not in known_cols and column not in measures:
            issues.append(DaxIssue("reference", f"Unknown column '{table}'[{column}]."))

    # Bare [Name] references: look at what remains after removing qualified refs.
    stripped = _QUALIFIED.sub(" ", expression)
    for name in _BARE.findall(stripped):
        if name not in all_columns and name not in measures:
            issues.append(DaxIssue("reference", f"Unknown reference [{name}]."))

    return issues
