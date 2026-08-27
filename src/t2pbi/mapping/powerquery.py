"""Generate Power Query (M) source expressions from Tableau connections.

Each Tableau connection class maps to a different M data-access function. This module produces
the ``source = ...`` body for a table partition. Unsupported classes fall back to an empty-table
stub and signal a warning so the migration report can flag them.
"""

from __future__ import annotations

from t2pbi.ir.tableau_ir import TableauConnection, TableauRelation

_EMPTY_TABLE = "let\n    Source = #table({}, {})\nin\n    Source"


def build_m_source(
    connection: TableauConnection | None, relation: TableauRelation
) -> tuple[str, str | None]:
    """Return ``(m_source, warning)`` for a relation. ``warning`` is None when fully mapped."""

    if connection is None:
        return _EMPTY_TABLE, "No connection resolved; emitted an empty-table stub."

    cls = connection.cls
    if cls == "textscan":
        return _csv(connection), None
    if cls == "excel-direct":
        return _excel(connection, relation), None
    if cls == "sqlserver":
        return _sql_server(connection, relation), None
    if cls == "postgres":
        return _db("PostgreSQL.Database", connection, relation, default_schema="public"), None
    if cls == "mysql":
        return _db("MySQL.Database", connection, relation, default_schema=connection.dbname), None

    return (
        _EMPTY_TABLE,
        f"Connection class '{cls}' not yet supported; emitted an empty-table stub.",
    )


def _csv(connection: TableauConnection) -> str:
    path = _file_path(connection)
    return (
        "let\n"
        f'    Source = Csv.Document(File.Contents("{_esc(path)}"), '
        "[Delimiter=\",\", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),\n"
        "    Headers = Table.PromoteHeaders(Source, [PromoteAllScalars=true])\n"
        "in\n"
        "    Headers"
    )


def _excel(connection: TableauConnection, relation: TableauRelation) -> str:
    path = _file_path(connection)
    sheet = relation.name
    # Match by name across any kind (Sheet/Table/DefinedName); a hardcoded Kind="Sheet" fails
    # when the workbook exposes the data as an Excel table or named range.
    return (
        "let\n"
        f'    Source = Excel.Workbook(File.Contents("{_esc(path)}"), null, true),\n'
        f'    Navigation = Table.SelectRows(Source, each [Name] = "{_esc(sheet)}"){{0}}[Data],\n'
        "    Headers = Table.PromoteHeaders(Navigation, [PromoteAllScalars=true])\n"
        "in\n"
        "    Headers"
    )


def _sql_server(connection: TableauConnection, relation: TableauRelation) -> str:
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
    connection: TableauConnection,
    relation: TableauRelation,
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


def _file_path(connection: TableauConnection) -> str:
    filename = connection.filename or ""
    directory = connection.directory
    if directory and not filename.startswith(("/", "\\")) and ":" not in filename:
        sep = "/" if "/" in directory else "\\"
        trimmed = directory.rstrip("/\\")
        return f"{trimmed}{sep}{filename}"
    return filename


def _schema_item(
    connection: TableauConnection,
    relation: TableauRelation,
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
