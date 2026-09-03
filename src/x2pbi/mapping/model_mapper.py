"""Deterministic mapping from the source IR to the Power BI IR.

Maps datasources to tables (one per relation), physical columns to typed columns with a real
Power Query partition, source joins to relationships, and calculated fields to measures with a
placeholder DAX body that the expression-translation stage fills in.

Source-agnostic: it reads only the normalized enums of ``x2pbi.ir.source_ir``, so it works for
any adapter without change.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from x2pbi.ir.powerbi_ir import (
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
from x2pbi.ir.source_ir import (
    Aggregation,
    ColumnOrigin,
    ConnectionKind,
    DataType,
    FieldRole,
    SourceCalculatedField,
    SourceColumn,
    SourceConnection,
    SourceDatasource,
    SourceRelation,
    SourceWorkbook,
)
from x2pbi.mapping.powerquery import build_m_source
from x2pbi.mapping.report import IssueSeverity, MigrationReport

_DATATYPE_MAP: dict[DataType, PBIDataType] = {
    DataType.INTEGER: PBIDataType.INT64,
    DataType.REAL: PBIDataType.DOUBLE,
    DataType.STRING: PBIDataType.STRING,
    DataType.BOOLEAN: PBIDataType.BOOLEAN,
    DataType.DATE: PBIDataType.DATE_TIME,
    DataType.DATETIME: PBIDataType.DATE_TIME,
    DataType.UNKNOWN: PBIDataType.STRING,
}

# Aggregations with no Power BI equivalent (Aggregation.OTHER, e.g. a date truncation) fall
# through to the SUM default below, matching how a measure with no declared default behaves.
_AGGREGATION_MAP: dict[Aggregation, SummarizeBy] = {
    Aggregation.SUM: SummarizeBy.SUM,
    Aggregation.COUNT: SummarizeBy.COUNT,
    Aggregation.COUNT_DISTINCT: SummarizeBy.COUNT,
    Aggregation.AVG: SummarizeBy.AVERAGE,
    Aggregation.MIN: SummarizeBy.MIN,
    Aggregation.MAX: SummarizeBy.MAX,
}

# Placeholder body for a measure whose DAX has not been translated yet. Valid DAX so the model
# still opens; the real expression is produced by the expression-translation stage.
UNTRANSLATED_DAX = "BLANK()"

# Connection kinds whose data is loaded from a file and needs strict type conversion.
_FILE_SOURCES = {ConnectionKind.CSV, ConnectionKind.EXCEL}


@dataclass
class MappingResult:
    """Output of the model-mapping stage."""

    model: PBIModel
    report: MigrationReport
    # Calculated fields awaiting DAX translation, keyed by "<table>::<measure>".
    pending_expressions: dict[str, SourceCalculatedField] = field(default_factory=dict)


def map_model(workbook: SourceWorkbook, report: MigrationReport | None = None) -> MappingResult:
    """Map a source workbook IR to a Power BI model IR (data-model structure only)."""

    report = report or MigrationReport()
    model = PBIModel(name=workbook.source_name)
    pending: dict[str, SourceCalculatedField] = {}

    for ds in workbook.datasources:
        tables, relationships = _map_datasource(ds, report, pending)
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

    return MappingResult(model=model, report=report, pending_expressions=pending)


def _map_datasource(
    ds: SourceDatasource,
    report: MigrationReport,
    pending: dict[str, SourceCalculatedField],
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
    table_sources: dict[str, tuple[SourceRelation, SourceConnection | None]] = {}
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
    _ensure_relationship_columns(tables, relationships)

    # Build partitions after columns (incl. hidden keys) are final so M types the whole table.
    for table in tables:
        relation, connection = table_sources[table.name]
        table.partition = _build_partition(relation, connection, table, report)

    return tables, relationships


def _ensure_relationship_columns(
    tables: list[PBITable], relationships: list[PBIRelationship]
) -> None:
    """Guarantee both key columns of each relationship exist, adding hidden ones if needed.

    A source tool may expose a join key on only one side, when the other side's key is not
    itself a workbook field; Power BI requires the column on both sides, so we add it (hidden)
    with a type mirrored from the known side.
    """

    by_name = {t.name: t for t in tables}
    for rel in relationships:
        from_table = by_name.get(rel.from_table)
        to_table = by_name.get(rel.to_table)
        _ensure_key_column(from_table, rel.from_column, to_table, rel.to_column)
        _ensure_key_column(to_table, rel.to_column, from_table, rel.from_column)


def _ensure_key_column(
    table: PBITable | None,
    column: str,
    other_table: PBITable | None,
    other_column: str,
) -> None:
    if table is None or any(c.name == column for c in table.columns):
        return
    data_type = _column_type(other_table, other_column) or PBIDataType.INT64
    table.columns.append(
        PBIColumn(
            name=column,
            data_type=data_type,
            source_column=column,
            summarize_by=SummarizeBy.NONE,
            is_hidden=True,
        )
    )


def _column_type(table: PBITable | None, column: str) -> PBIDataType | None:
    if table is None:
        return None
    return next((c.data_type for c in table.columns if c.name == column), None)


def _synthetic_relation(ds: SourceDatasource) -> SourceRelation:
    """Build a single relation for datasources that expose no explicit relation of their own."""

    conn = ds.connections[0] if ds.connections else None
    kind = "text" if conn and conn.kind in _FILE_SOURCES else "table"
    return SourceRelation(
        name=ds.display_name,
        kind=kind,
        connection_name=conn.name if conn else None,
    )


def _map_measures(
    table_name: str,
    ds: SourceDatasource,
    pending: dict[str, SourceCalculatedField],
) -> list[PBIMeasure]:
    return [_map_calculated_field(table_name, calc, pending) for calc in ds.calculated_fields]


def _map_joins(
    ds: SourceDatasource, single_table: bool, report: MigrationReport
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
            "Relationship inferred from a join in the source workbook; verify cardinality "
            "and filter direction.",
            detail=f"{join.left_table}[{join.left_column}] {join.join_type} {join.right_table}[{join.right_column}]",
        )
    return relationships


def _map_column(col: SourceColumn, origin: ColumnOrigin | None) -> PBIColumn:
    data_type = _DATATYPE_MAP.get(col.datatype, PBIDataType.STRING)
    summarize = SummarizeBy.NONE
    if col.role is FieldRole.MEASURE:
        summarize = _AGGREGATION_MAP.get(col.aggregation, SummarizeBy.SUM)
    source_column = origin.source_column if origin else col.display_name
    return PBIColumn(
        name=col.display_name,
        data_type=data_type,
        source_column=source_column,
        summarize_by=summarize,
    )


def _map_calculated_field(
    table_name: str,
    calc: SourceCalculatedField,
    pending: dict[str, SourceCalculatedField],
) -> PBIMeasure:
    measure = PBIMeasure(
        name=calc.display_name,
        expression=UNTRANSLATED_DAX,
        description=f"TODO: translate from source calc: {calc.formula}",
    )
    pending[f"{table_name}::{calc.display_name}"] = calc
    return measure


def _build_partition(
    relation: SourceRelation,
    connection: SourceConnection | None,
    table: PBITable,
    report: MigrationReport,
) -> PBIPartition:
    """Build a table partition with a real Power Query source when the connection is supported."""

    m_source, warning = build_m_source(connection, relation, table.columns)
    if warning:
        report.add(IssueSeverity.WARNING, "model", table.name, warning)
    elif connection is not None and connection.kind in _FILE_SOURCES:
        # File data is typed strictly (matching the source's declared types); Power BI surfaces
        # any unconvertible values as an "Errors in <table>" query rather than silently nulling them.
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
