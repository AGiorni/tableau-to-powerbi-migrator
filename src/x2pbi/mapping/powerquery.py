"""Generate Power Query (M) source expressions from source connections.

Each :class:`~x2pbi.ir.source_ir.ConnectionKind` maps to a different M data-access function.
This module produces the ``source = ...`` body for a table partition. Unsupported kinds fall
back to an empty-table stub and signal a warning so the migration report can flag them.

Source-agnostic: adapters normalize their own connection classes to ``ConnectionKind``, so a new
source gets working partitions for every technology already listed here.
"""

from __future__ import annotations

from x2pbi.ir.powerbi_ir import PBIColumn, PBIDataType
from x2pbi.ir.source_ir import ConnectionKind, SourceConnection, SourceRelation

_EMPTY_TABLE = "let\n    Source = #table({}, {})\nin\n    Source"

# Model data type -> M ascription used in Table.TransformColumnTypes.
_M_TYPES: dict[PBIDataType, str] = {
    PBIDataType.INT64: "Int64.Type",
    PBIDataType.DOUBLE: "type number",
    PBIDataType.DECIMAL: "type number",
    PBIDataType.STRING: "type text",
    PBIDataType.BOOLEAN: "type logical",
    PBIDataType.DATE_TIME: "type datetime",
}


def build_m_source(
    connection: SourceConnection | None,
    relation: SourceRelation,
    columns: list[PBIColumn] | None = None,
) -> tuple[str, str | None]:
    """Return ``(m_source, warning)`` for a relation. ``warning`` is None when fully mapped."""

    columns = columns or []
    if connection is None:
        return _EMPTY_TABLE, "No connection resolved; emitted an empty-table stub."

    kind = connection.kind
    if kind is ConnectionKind.CSV:
        return _csv(connection, columns), None
    if kind is ConnectionKind.EXCEL:
        return _excel(connection, relation, columns), None
    if kind is ConnectionKind.SQL_SERVER:
        return _sql_server(connection, relation), None
    if kind is ConnectionKind.POSTGRES:
        return _db("PostgreSQL.Database", connection, relation, default_schema="public"), None
    if kind is ConnectionKind.MYSQL:
        return _db("MySQL.Database", connection, relation, default_schema=connection.dbname), None

    described = connection.native_class or kind.value
    return (
        _EMPTY_TABLE,
        f"Connection type '{described}' not yet supported; emitted an empty-table stub.",
    )


def _csv(connection: SourceConnection, columns: list[PBIColumn]) -> str:
    path = _file_path(connection)
    steps = [
        (
            f'Source = Csv.Document(File.Contents("{_esc(path)}"), '
            '[Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv])'
        ),
        "Headers = Table.PromoteHeaders(Source, [PromoteAllScalars=true])",
    ]
    return _finish(steps, "Headers", columns)


def _excel(
    connection: SourceConnection, relation: SourceRelation, columns: list[PBIColumn]
) -> str:
    path = _file_path(connection)
    sheet = relation.name
    # Match by name across any kind (Sheet/Table/DefinedName); a hardcoded Kind="Sheet" fails
    # when the workbook exposes the data as an Excel table or named range.
    steps = [
        f'Source = Excel.Workbook(File.Contents("{_esc(path)}"), null, true)',
        f'Navigation = Table.SelectRows(Source, each [Name] = "{_esc(sheet)}"){{0}}[Data]',
        "Headers = Table.PromoteHeaders(Navigation, [PromoteAllScalars=true])",
    ]
    return _finish(steps, "Headers", columns)


def _finish(steps: list[str], last_step: str, columns: list[PBIColumn]) -> str:
    """Assemble a let/in query, adding a type-transform step so loaded types match the model.

    File sources (CSV/Excel) load every column as text after PromoteHeaders; without this the
    model column would render as text despite its declared data type.
    """

    transform = _type_transform(columns, last_step)
    if transform:
        steps.append(transform)
        last_step = "Typed"
    body = ",\n    ".join(steps)
    return f"let\n    {body}\nin\n    {last_step}"


def _type_transform(columns: list[PBIColumn], source_step: str) -> str:
    pairs = [
        f'{{"{_esc(c.source_column)}", {_M_TYPES.get(c.data_type, "type any")}}}' for c in columns
    ]
    if not pairs:
        return ""
    return f"Typed = Table.TransformColumnTypes({source_step}, {{{', '.join(pairs)}}})"


def _sql_server(connection: SourceConnection, relation: SourceRelation) -> str:
    schema, item = _schema_item(connection, relation)
    server = _esc(connection.server or "")
    database = _esc(connection.dbname or "")
    return (
        "let\n"
        f'    Source = Sql.Database("{server}", "{database}"),\n'
        f'    Navigation = Source{{[Schema="{_esc(schema)}",Item="{_esc(item)}"]}}[Data]\n'
        "in\n"
        "    Navigation"
    )


def _db(
    func: str,
    connection: SourceConnection,
    relation: SourceRelation,
    default_schema: str | None,
) -> str:
    schema, item = _schema_item(connection, relation, default_schema=default_schema)
    server = _esc(connection.server or "")
    database = _esc(connection.dbname or "")
    return (
        "let\n"
        f'    Source = {func}("{server}", "{database}"),\n'
        f'    Navigation = Source{{[Schema="{_esc(schema)}",Item="{_esc(item)}"]}}[Data]\n'
        "in\n"
        "    Navigation"
    )


def _file_path(connection: SourceConnection) -> str:
    filename = connection.filename or ""
    directory = connection.directory
    if directory and not filename.startswith(("/", "\\")) and ":" not in filename:
        sep = "/" if "/" in directory else "\\"
        trimmed = directory.rstrip("/\\")
        return f"{trimmed}{sep}{filename}"
    return filename


def _schema_item(
    connection: SourceConnection,
    relation: SourceRelation,
    default_schema: str | None = None,
) -> tuple[str, str]:
    """Resolve (schema, table) from a relation's '[schema].[table]' id."""

    default_schema = default_schema or connection.schema_name or "dbo"
    table = relation.table
    if table and "].[" in table:
        left, _, right = table.partition("].[")
        return _unbracket(left + "]"), _unbracket("[" + right)
    if table:
        return default_schema, _unbracket(table)
    return default_schema, relation.name


def _unbracket(name: str) -> str:
    return name[1:-1] if name.startswith("[") and name.endswith("]") else name


def _esc(text: str) -> str:
    """Escape a value for an M double-quoted string literal."""

    return text.replace('"', '""')
