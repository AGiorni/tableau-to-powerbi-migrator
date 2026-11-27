"""Unpack the data a packaged workbook carries, so the migrated report has data to load.

A ``.twbx`` downloaded from Tableau Public holds its data only as a Hyper extract: the Excel or
CSV file it was built from stays on its author's machine, and the workbook's connection still
names that path. This exports every table of every extract inside the package to a CSV file in
the output folder (``<out>/data/``), which the model mapper then reads instead of the dead path.

It uses Tableau's Hyper API (``tableauhyperapi``, the optional ``extract`` extra), with Tableau's
usage telemetry turned off: the data stays on this machine. Without the package, nothing is
exported and the caller reports why. A plain ``.twb`` is never touched: its extract, if any, is a
file elsewhere (often a temp file), and its source connection is what it means.
"""

from __future__ import annotations

import re
import tempfile
import zipfile
from pathlib import Path

from pydantic import BaseModel

from t2pbi.ir.tableau_ir import TableauWorkbook

# Tableau names a logical table's extract table '<table>_<32 hex digits>'.
_OBJECT_ID = re.compile(r"^(.*)_[0-9A-Fa-f]{32}$")


class ExportedTable(BaseModel):
    """One extract table written as CSV: where, its header, and how many rows."""

    path: str
    columns: list[str]
    rows: int


class ExtractsUnavailable(RuntimeError):
    """The workbook has packaged extracts, but they can't be read here (why is in the message)."""


def packaged_extracts(workbook: TableauWorkbook, package: str | Path) -> dict[str, str]:
    """Datasource name -> the path inside the package of its extract, for extracts the package
    actually contains. Empty for a ``.twb`` or a package without them."""

    package = Path(package)
    if package.suffix.lower() != ".twbx":
        return {}
    with zipfile.ZipFile(package) as archive:
        names = set(archive.namelist())
    return {ds.name: ds.extract.path for ds in workbook.datasources if ds.extract and ds.extract.path in names}


def export_extracts(
    workbook: TableauWorkbook, package: str | Path, out_dir: str | Path
) -> dict[str, dict[str, ExportedTable]]:
    """Export every table of every packaged extract to ``<out_dir>/data/``.

    Returns datasource name -> extract table name -> what was written. Raises
    :class:`ExtractsUnavailable` when there are extracts but the Hyper API isn't installed.
    """

    wanted = packaged_extracts(workbook, package)
    if not wanted:
        return {}
    try:
        from tableauhyperapi import (
            Connection,
            HyperProcess,
            TableName,
            Telemetry,
            escape_string_literal,
        )
    except ImportError:
        raise ExtractsUnavailable(
            "the workbook's data is in a Tableau extract, and reading it needs Tableau's Hyper API: "
            'pip install -e ".[extract]"'
        ) from None

    data_dir = Path(out_dir).resolve() / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    exported: dict[str, dict[str, ExportedTable]] = {}
    captions = {ds.name: ds.display_name for ds in workbook.datasources}
    with tempfile.TemporaryDirectory() as work, zipfile.ZipFile(package) as archive, \
            HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU,
                         # Hyper logs every file path it opens; keep that log out of the
                         # user's folders (it lands in the cwd otherwise) and delete it.
                         parameters={"log_dir": work}) as hyper:
        for ds_name, inner_path in wanted.items():
            database = archive.extract(inner_path, work)
            tables: dict[str, ExportedTable] = {}
            with Connection(hyper.endpoint, database) as conn:
                for table in conn.catalog.get_table_names("Extract"):
                    name = table.name.unescaped
                    target = data_dir / f"{_file_part(captions[ds_name])}__{_file_part(_strip_object_id(name))}.csv"
                    columns = [c.name.unescaped for c in conn.catalog.get_table_definition(table).columns]
                    conn.execute_command(
                        f"COPY {TableName('Extract', name)} TO {escape_string_literal(str(target))} "
                        "WITH (FORMAT csv, HEADER)"
                    )
                    rows = conn.execute_scalar_query(f"SELECT COUNT(*) FROM {TableName('Extract', name)}")
                    tables[name] = ExportedTable(path=str(target), columns=columns, rows=int(rows))
            exported[ds_name] = tables
    return exported


def table_for(relation_name: str, tables: dict[str, ExportedTable], single_table: bool) -> ExportedTable | None:
    """The exported table holding a relation's data: the one named after its logical table
    ('Orders_ECFC…' for 'Orders'), or the only one when the datasource is one table."""

    for name, table in tables.items():
        if _strip_object_id(name) == relation_name:
            return table
    if single_table and len(tables) == 1:
        return next(iter(tables.values()))
    return None


def _strip_object_id(name: str) -> str:
    match = _OBJECT_ID.match(name)
    return match.group(1) if match else name


def _file_part(name: str) -> str:
    """A name safe in a file name (Windows forbids <>:"/\\|?*)."""

    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .") or "table"
