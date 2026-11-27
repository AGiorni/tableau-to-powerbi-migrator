"""Deterministic mapping from the Tableau IR to the Power BI IR.

Maps datasources to tables (one per relation), physical columns to typed columns with a real
Power Query partition, Tableau joins to relationships, and calculated fields to measures with a
placeholder DAX body that the expression-translation stage fills in.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from t2pbi.ingest.extracts import ExportedTable, table_for
from t2pbi.ir.powerbi_ir import (
    Cardinality,
    CrossFilter,
    PBIColumn,
    PBIDataType,
    PBIMeasure,
    PBIModel,
    PBIPartition,
    PBIRelationship,
    PBITable,
    SummarizeBy,
)
from t2pbi.ir.tableau_ir import (
    ColumnOrigin,
    TableauCalculatedField,
    TableauColumn,
    TableauConnection,
    TableauDatasource,
    TableauDataType,
    TableauRelation,
    TableauRole,
    TableauWorkbook,
    counted_table,
    strip_brackets,
)
from t2pbi.mapping.powerquery import build_m_source, csv_file_source
from t2pbi.mapping.report import IssueSeverity, MigrationReport

_DATATYPE_MAP: dict[TableauDataType, PBIDataType] = {
    TableauDataType.INTEGER: PBIDataType.INT64,
    TableauDataType.REAL: PBIDataType.DOUBLE,
    TableauDataType.STRING: PBIDataType.STRING,
    TableauDataType.BOOLEAN: PBIDataType.BOOLEAN,
    TableauDataType.DATE: PBIDataType.DATE_TIME,
    TableauDataType.DATETIME: PBIDataType.DATE_TIME,
    TableauDataType.UNKNOWN: PBIDataType.STRING,
}

_AGGREGATION_MAP: dict[str, SummarizeBy] = {
    "Sum": SummarizeBy.SUM,
    "Count": SummarizeBy.COUNT,
    "CountD": SummarizeBy.COUNT,
    "Avg": SummarizeBy.AVERAGE,
    "Min": SummarizeBy.MIN,
    "Max": SummarizeBy.MAX,
}

# Placeholder body for a measure whose DAX has not been translated yet. Valid DAX so the model
# still opens; the real expression is produced by the expression-translation stage.
UNTRANSLATED_DAX = "BLANK()"

# Connection classes whose data is loaded from a file and needs strict type conversion.
_FILE_SOURCES = {"textscan", "excel-direct"}


@dataclass
class MappingResult:
    """Output of the model-mapping stage."""

    model: PBIModel
    # Calculated fields awaiting DAX translation, keyed by "<table>::<measure>".
    pending_expressions: dict[str, TableauCalculatedField] = field(default_factory=dict)


def map_model(
    workbook: TableauWorkbook,
    report: MigrationReport,
    extracts: dict[str, dict[str, ExportedTable]] | None = None,
) -> MappingResult:
    """Map a Tableau workbook IR to a Power BI model IR (data-model structure only).

    ``extracts``: the CSV files a packaged workbook's extracts were unpacked to (datasource name
    -> extract table -> file). A table they cover loads from its file instead of the workbook's
    own connection, which points at the author's machine."""

    model = PBIModel(name=workbook.source_name)
    pending: dict[str, TableauCalculatedField] = {}

    for ds in workbook.datasources:
        tables, relationships = _map_datasource(ds, report, pending, (extracts or {}).get(ds.name, {}))
        kept = [t for t in tables if t.columns or t.measures]
        for skipped in (t for t in tables if not (t.columns or t.measures)):
            report.add(
                IssueSeverity.WARNING,
                "model",
                skipped.name,
                "Table produced no columns or measures and was skipped.",
            )
        model.tables.extend(kept)
        kept_names = {t.name for t in kept}
        model.relationships.extend(
            r for r in relationships if r.from_table in kept_names and r.to_table in kept_names
        )

    _add_row_count_measures(workbook, model, report)
    return MappingResult(model=model, pending_expressions=pending)


def row_count_measure_name(counted: str) -> str:
    """The measure added for Tableau's row count of logical table ``counted``, named as Tableau's
    data pane shows that field ("Orders (Count)")."""

    return f"{counted} (Count)"


def _add_row_count_measures(workbook: TableauWorkbook, model: PBIModel, report: MigrationReport) -> None:
    """Tableau counts a logical table's rows with a built-in field; Power BI has none, so each
    table that a sheet counts gets a ``COUNTROWS`` measure (a count of one column would miss its
    blank rows)."""

    wanted: list[str] = []
    for ws in workbook.worksheets:
        for ref in [*ws.rows, *ws.cols, *ws.measure_names, *(e.field for e in ws.encodings)]:
            table = counted_table(ref)
            if table is not None and table not in wanted:
                wanted.append(table)
    existing = {m.name for t in model.tables for m in t.measures}
    for counted in wanted:
        target = _count_target(counted, model)
        if target is None:
            report.add(
                IssueSeverity.WARNING, "model", counted,
                f"Tableau counts the rows of {counted!r}, but no migrated table matches it; the count is left out.",
            )
            continue
        name = row_count_measure_name(counted)
        if name in existing:
            continue
        quoted = target.name.replace("'", "''")
        target.measures.append(PBIMeasure(name=name, expression=f"COUNTROWS('{quoted}')", format_string="#,0"))
        existing.add(name)


def _count_target(counted: str, model: PBIModel) -> PBITable | None:
    """The table a Tableau row count refers to: the one named after its logical table, or the
    model's only table (a single-table datasource is named after the datasource instead)."""

    for table in model.tables:
        if table.name in (counted, _sanitize_name(counted)):
            return table
    return model.tables[0] if len(model.tables) == 1 else None


def _map_datasource(
    ds: TableauDatasource,
    report: MigrationReport,
    pending: dict[str, TableauCalculatedField],
    exported: dict[str, ExportedTable] | None = None,
) -> tuple[list[PBITable], list[PBIRelationship]]:
    """Map one datasource to one or more tables (one per relation) plus relationships."""

    relations = ds.relations or [_synthetic_relation(ds)]
    conn_by_name = {c.name: c for c in ds.connections if c.name}
    sole_conn = ds.connections[0] if len(ds.connections) == 1 else None
    single_table = len(relations) <= 1
    primary = relations[0].name

    # Group physical columns by the relation they originate from.
    grouped: dict[str, list[PBIColumn]] = {r.name: [] for r in relations}
    for col in ds.columns:
        if col.hidden:
            continue
        origin = ds.column_origins.get(col.name)
        target = origin.table if origin and origin.table in grouped else primary
        grouped[target].append(_map_column(col, origin))

    tables: list[PBITable] = []
    # Partition sources are built last (see below) so type transforms include hidden key columns.
    table_sources: dict[str, tuple[TableauRelation, TableauConnection | None]] = {}
    for relation in relations:
        table_name = _sanitize_name(ds.display_name if single_table else relation.name)
        columns = grouped.get(relation.name, [])
        measures = _map_measures(table_name, ds, pending) if relation.name == primary else []
        tables.append(PBITable(name=table_name, columns=columns, measures=measures))
        table_sources[table_name] = (
            relation,
            conn_by_name.get(relation.connection_name) or sole_conn,
        )

    relationships = _map_joins(ds, single_table, report)
    sources = {strip_brackets(name): origin.source_column for name, origin in ds.column_origins.items()
               if origin.source_column}
    _ensure_relationship_columns(tables, relationships, sources)

    # Build partitions after columns (incl. hidden keys) are final so M types the whole table.
    for table in tables:
        relation, connection = table_sources[table.name]
        unpacked = table_for(relation.name, exported, single_table) if exported else None
        if unpacked is not None:
            table.partition = _extract_partition(table, unpacked, report)
        else:
            if exported:
                report.add(
                    IssueSeverity.WARNING, "model", table.name,
                    "The workbook's extract has no table for this one; it keeps the workbook's own "
                    "connection, which points at the author's files.",
                )
            table.partition = _build_partition(relation, connection, table, report)

    return tables, relationships


def _extract_partition(table: PBITable, unpacked: ExportedTable, report: MigrationReport) -> PBIPartition:
    """Load a table from the CSV its extract table was unpacked to. Each column is matched to
    the file's header by its source name, else its own name; one the file lacks stays in the
    model, filled with blanks, and is reported."""

    header = set(unpacked.columns)
    present, absent = [], []
    for i, column in enumerate(table.columns):
        if column.source_column not in header and column.name in header:
            column = column.model_copy(update={"source_column": column.name})
            table.columns[i] = column
        (present if column.source_column in header else absent).append(column)
    if absent:
        report.add(
            IssueSeverity.WARNING, "model", table.name,
            "Not in the workbook's extract, so left blank: " + ", ".join(c.name for c in absent) + ".",
        )
    report.add(
        IssueSeverity.INFO, "model", table.name,
        f"Loads from the data packaged in the workbook ({Path(unpacked.path).name}, {unpacked.rows:,} rows).",
    )
    return PBIPartition(name=f"{table.name}-partition", m_source=csv_file_source(unpacked.path, present, absent))


def _ensure_relationship_columns(
    tables: list[PBITable], relationships: list[PBIRelationship], sources: dict[str, str] | None = None
) -> None:
    """Guarantee both key columns of each relationship exist, adding hidden ones if needed.

    Tableau exposes a join key on only one side when the other side's key is not a workbook
    field (or is a hidden one); Power BI requires the column on both sides, so we add it (hidden)
    with a type mirrored from the known side. ``sources``: Tableau column name -> its column in
    the source. A key Tableau renamed to tell it from another table's ("Order ID (Returns)") is
    still "Order ID" in the source, and that's the column the table must load.
    """

    by_name = {t.name: t for t in tables}
    for rel in relationships:
        from_table = by_name.get(rel.from_table)
        to_table = by_name.get(rel.to_table)
        _ensure_key_column(from_table, rel.from_column, to_table, rel.to_column, sources or {})
        _ensure_key_column(to_table, rel.to_column, from_table, rel.from_column, sources or {})


def _ensure_key_column(
    table: PBITable | None,
    column: str,
    other_table: PBITable | None,
    other_column: str,
    sources: dict[str, str],
) -> None:
    if table is None or any(c.name == column for c in table.columns):
        return
    data_type = _column_type(other_table, other_column) or PBIDataType.INT64
    table.columns.append(
        PBIColumn(
            name=column,
            data_type=data_type,
            source_column=sources.get(column, column),
            summarize_by=SummarizeBy.NONE,
            is_hidden=True,
        )
    )


def _column_type(table: PBITable | None, column: str) -> PBIDataType | None:
    if table is None:
        return None
    return next((c.data_type for c in table.columns if c.name == column), None)


def _synthetic_relation(ds: TableauDatasource) -> TableauRelation:
    """Build a single relation for datasources that expose no explicit <relation> element."""

    conn = ds.connections[0] if ds.connections else None
    return TableauRelation(name=ds.display_name, connection_name=conn.name if conn else None)


def _map_measures(
    table_name: str,
    ds: TableauDatasource,
    pending: dict[str, TableauCalculatedField],
) -> list[PBIMeasure]:
    return [_map_calculated_field(table_name, calc, pending) for calc in ds.calculated_fields]


def _map_joins(
    ds: TableauDatasource, single_table: bool, report: MigrationReport
) -> list[PBIRelationship]:
    if single_table or not ds.joins:
        return []
    relationships: list[PBIRelationship] = []
    for join in ds.joins:
        from_table = _sanitize_name(join.left_table)
        to_table = _sanitize_name(join.right_table)
        relationships.append(
            PBIRelationship(
                name=str(uuid.uuid4()),
                from_table=from_table,
                from_column=join.left_column,
                to_table=to_table,
                to_column=join.right_column,
                cardinality=Cardinality.MANY_TO_ONE,
                cross_filter=CrossFilter.SINGLE,
            )
        )
        report.add(
            IssueSeverity.WARNING,
            "model",
            f"{from_table} -> {to_table}",
            "Relationship inferred from a Tableau join; verify cardinality and filter direction.",
            detail=f"{join.left_table}[{join.left_column}] {join.join_type} {join.right_table}[{join.right_column}]",
        )
    return relationships


def _map_column(col: TableauColumn, origin: ColumnOrigin | None) -> PBIColumn:
    data_type = _DATATYPE_MAP.get(col.datatype, PBIDataType.STRING)
    summarize = SummarizeBy.NONE
    if col.role is TableauRole.MEASURE:
        summarize = _AGGREGATION_MAP.get(col.aggregation or "", SummarizeBy.SUM)
    source_column = origin.source_column if origin else col.display_name
    return PBIColumn(
        name=col.display_name,
        data_type=data_type,
        source_column=source_column,
        summarize_by=summarize,
    )


def _map_calculated_field(
    table_name: str,
    calc: TableauCalculatedField,
    pending: dict[str, TableauCalculatedField],
) -> PBIMeasure:
    measure = PBIMeasure(
        name=calc.display_name,
        expression=UNTRANSLATED_DAX,
        description=f"TODO: translate from Tableau calc: {calc.formula}",
    )
    pending[f"{table_name}::{calc.display_name}"] = calc
    return measure


def _build_partition(
    relation: TableauRelation,
    connection: TableauConnection | None,
    table: PBITable,
    report: MigrationReport,
) -> PBIPartition:
    """Build a table partition with a real Power Query source when the connection is supported."""

    m_source, warning = build_m_source(connection, relation, table.columns)
    if warning:
        report.add(IssueSeverity.WARNING, "model", table.name, warning)
    elif connection is not None and connection.cls in _FILE_SOURCES:
        # File data is typed strictly (matching Tableau's declared types); Power BI surfaces any
        # unconvertible values as an "Errors in <table>" query rather than silently nulling them.
        report.add(
            IssueSeverity.INFO,
            "model",
            table.name,
            f"Columns are type-converted from a file source; review the 'Errors in {table.name}' "
            "query in Power BI for values that don't match their migrated type (e.g. codes typed "
            "as numbers).",
        )
    return PBIPartition(name=f"{table.name}-partition", m_source=m_source)


def _sanitize_name(name: str) -> str:
    """Make a name safe for use as a TMDL/TOM object name."""

    cleaned = re.sub(r"\s+", " ", name).strip()
    return cleaned or f"Table_{uuid.uuid4().hex[:8]}"
