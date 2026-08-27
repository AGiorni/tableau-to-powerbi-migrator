"""Structural validation of a Power BI model IR before it is written to disk."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Literal

from t2pbi.ir.powerbi_ir import PBIModel


@dataclass(frozen=True)
class Finding:
    severity: Literal["error", "warning"]
    object_name: str
    message: str


def validate_model(model: PBIModel) -> list[Finding]:
    """Return structural problems that would break the emitted semantic model."""

    findings: list[Finding] = []
    columns_by_table = {t.name: {c.name for c in t.columns} for t in model.tables}

    _check_duplicate_tables(model, findings)
    _check_partitions(model, findings)
    _check_relationships(model, columns_by_table, findings)
    return findings


def _check_duplicate_tables(model: PBIModel, findings: list[Finding]) -> None:
    for name, count in Counter(t.name for t in model.tables).items():
        if count > 1:
            findings.append(Finding("error", name, f"Duplicate table name '{name}' ({count}x)."))


def _check_partitions(model: PBIModel, findings: list[Finding]) -> None:
    for table in model.tables:
        if table.partition is None:
            findings.append(
                Finding("error", table.name, f"Table '{table.name}' has no partition (no data source).")
            )


def _check_relationships(
    model: PBIModel, columns_by_table: dict[str, set[str]], findings: list[Finding]
) -> None:
    for rel in model.relationships:
        for table, column, side in (
            (rel.from_table, rel.from_column, "from"),
            (rel.to_table, rel.to_column, "to"),
        ):
            cols = columns_by_table.get(table)
            if cols is None:
                findings.append(
                    Finding("error", rel.name, f"Relationship {side} table '{table}' does not exist.")
                )
            elif column not in cols:
                findings.append(
                    Finding(
                        "error",
                        rel.name,
                        f"Relationship {side} column '{table}'[{column}] does not exist.",
                    )
                )
