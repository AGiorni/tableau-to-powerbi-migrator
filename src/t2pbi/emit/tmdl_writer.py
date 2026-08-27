"""Render a Power BI IR model as TMDL text files.

TMDL is a YAML-like, tab-indented format. This writer follows the grammar documented for
Tabular Model Definition Language: one file per table under ``tables/``, plus root
``database``, ``model``, and ``relationships`` files. Object names are single-quoted when they
contain whitespace or reserved characters.
"""

from __future__ import annotations

from t2pbi.ir.powerbi_ir import (
    PBIColumn,
    PBIMeasure,
    PBIModel,
    PBIRelationship,
    PBITable,
)

_TAB = "\t"
_QUOTE_TRIGGERS = set(" .=:'")


def render_semantic_model(model: PBIModel) -> dict[str, str]:
    """Return a mapping of ``definition/``-relative file paths to TMDL text."""

    files: dict[str, str] = {
        "database.tmdl": _render_database(model),
        "model.tmdl": _render_model(model),
    }
    for table in model.tables:
        files[f"tables/{table.name}.tmdl"] = _render_table(table)
    if model.relationships:
        files["relationships.tmdl"] = _render_relationships(model.relationships)
    return files


def _render_database(model: PBIModel) -> str:
    return f"database {_name(model.name)}\n\tcompatibilityLevel: {model.compatibility_level}\n"


def _render_model(model: PBIModel) -> str:
    lines = [
        f"model {_name(model.name)}",
        f"{_TAB}culture: {model.culture}",
        # Marks the model as enhanced (V3) metadata, required for modeling changes in Desktop.
        f"{_TAB}defaultPowerBIDataSourceVersion: powerBI_V3",
        "",
    ]
    for table in model.tables:
        lines.append(f"ref table {_name(table.name)}")
    lines.append("")
    return "\n".join(lines)


def _render_table(table: PBITable) -> str:
    lines: list[str] = [f"table {_name(table.name)}"]
    if table.is_hidden:
        lines.append(f"{_TAB}isHidden")
    lines.append("")

    for column in table.columns:
        lines.extend(_render_column(column))
        lines.append("")

    for measure in table.measures:
        lines.extend(_render_measure(measure))
        lines.append("")

    if table.partition is not None:
        lines.extend(_render_partition(table))
        lines.append("")

    return "\n".join(lines)


def _render_column(column: PBIColumn) -> list[str]:
    lines = [f"{_TAB}column {_name(column.name)}"]
    lines.append(f"{_TAB}{_TAB}dataType: {column.data_type.value}")
    if column.is_hidden:
        lines.append(f"{_TAB}{_TAB}isHidden")
    lines.append(f"{_TAB}{_TAB}summarizeBy: {column.summarize_by.value}")
    lines.append(f"{_TAB}{_TAB}sourceColumn: {_quote_value(column.source_column)}")
    if column.format_string:
        lines.append(f"{_TAB}{_TAB}formatString: {column.format_string}")
    return lines


def _render_measure(measure: PBIMeasure) -> list[str]:
    lines: list[str] = []
    if measure.description:
        for desc_line in measure.description.splitlines() or [""]:
            lines.append(f"{_TAB}/// {desc_line}")
    lines.extend(_render_expression_property(f"measure {_name(measure.name)}", measure.expression, 1))
    if measure.format_string:
        lines.append(f"{_TAB}{_TAB}formatString: {measure.format_string}")
    if measure.display_folder:
        lines.append(f"{_TAB}{_TAB}displayFolder: {measure.display_folder}")
    if measure.is_hidden:
        lines.append(f"{_TAB}{_TAB}isHidden")
    return lines


def _render_partition(table: PBITable) -> list[str]:
    partition = table.partition
    assert partition is not None
    lines = [f"{_TAB}partition {_name(partition.name)} = m"]
    lines.append(f"{_TAB}{_TAB}mode: {partition.mode}")
    lines.extend(_render_expression_body(f"{_TAB}{_TAB}source =", partition.m_source, base_indent=3))
    return lines


def _render_relationships(relationships: list[PBIRelationship]) -> str:
    blocks: list[str] = []
    for rel in relationships:
        lines = [f"relationship {_name(rel.name)}"]
        lines.append(f"{_TAB}fromColumn: {_name(rel.from_table)}.{_name(rel.from_column)}")
        lines.append(f"{_TAB}toColumn: {_name(rel.to_table)}.{_name(rel.to_column)}")
        if rel.cardinality.value != "manyToOne":
            lines.append(f"{_TAB}fromCardinality: {rel.cardinality.value}")
        if rel.cross_filter.value != "oneDirection":
            lines.append(f"{_TAB}crossFilteringBehavior: {rel.cross_filter.value}")
        if not rel.is_active:
            lines.append(f"{_TAB}isActive: false")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def _render_expression_property(header: str, expression: str, indent: int) -> list[str]:
    """Render a default-property expression (``header = <expr>``) at the given indent level."""

    prefix = _TAB * indent
    if "\n" not in expression:
        return [f"{prefix}{header} = {expression}"]
    lines = [f"{prefix}{header} ="]
    lines.extend(_render_expression_body("", expression, base_indent=indent + 1))
    return lines


def _render_expression_body(header: str, expression: str, base_indent: int) -> list[str]:
    """Render a multi-line expression body indented one level deeper than its property."""

    prefix = _TAB * base_indent
    lines: list[str] = []
    if header:
        lines.append(header)
    for expr_line in expression.splitlines():
        lines.append(f"{prefix}{expr_line}" if expr_line else "")
    return lines


def _name(name: str) -> str:
    """Quote a TMDL object name if it contains whitespace or reserved characters."""

    if any(ch in _QUOTE_TRIGGERS for ch in name):
        escaped = name.replace("'", "''")
        return f"'{escaped}'"
    return name


def _quote_value(value: str) -> str:
    """Quote a text property value if it has leading/trailing whitespace or reserved chars."""

    if value != value.strip() or any(ch in _QUOTE_TRIGGERS for ch in value):
        escaped = value.replace('"', '""')
        return f'"{escaped}"'
    return value
