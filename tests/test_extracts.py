"""A packaged workbook's data: its Tableau extract, unpacked to CSV for the migrated model.

The package is built here from the committed superstore sample: its XML gets an extract, and a
small Hyper database is made with Tableau's Hyper API (skipped when that isn't installed).
"""

from __future__ import annotations

import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest

from t2pbi.graph import MigrationContext, migrate
from t2pbi.ingest import load_workbook
from t2pbi.parse import parse_workbook

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
_EXTRACT = (
    "      <extract enabled='true'>\n"
    "        <connection class='hyper' dbname='Data/Extracts/orders.hyper' schema='Extract' tablename='Extract' />\n"
    "        <relation name='Extract' table='[Extract].[Extract]' type='table' />\n"
    "      </extract>\n"
)


def _packaged_superstore(tmp_path: Path) -> Path:
    """superstore.twb with an extract, packaged with a 3-row Hyper file (no Quantity column)."""

    hyperapi = pytest.importorskip("tableauhyperapi")
    hyper_file = tmp_path / "orders.hyper"
    with hyperapi.HyperProcess(telemetry=hyperapi.Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU,
                               parameters={"log_dir": str(tmp_path)}) as hyper, \
            hyperapi.Connection(hyper.endpoint, str(hyper_file), hyperapi.CreateMode.CREATE_AND_REPLACE) as conn:
        conn.catalog.create_schema("Extract")
        table = hyperapi.TableDefinition(hyperapi.TableName("Extract", "Extract"), [
            hyperapi.TableDefinition.Column("Sales", hyperapi.SqlType.double()),
            hyperapi.TableDefinition.Column("Category", hyperapi.SqlType.text()),
            hyperapi.TableDefinition.Column("Order Date", hyperapi.SqlType.date()),
        ])
        conn.catalog.create_table(table)
        with hyperapi.Inserter(conn, table) as inserter:
            inserter.add_rows([[10.5, "Technology", date(2024, 1, 31)], [3.0, "Furniture", date(2024, 2, 1)],
                               [7.25, "Office Supplies", date(2024, 2, 2)]])
            inserter.execute()
    twb = (SAMPLES / "superstore" / "superstore.twb").read_text(encoding="utf-8")
    anchor = "      </connection>\n      <column caption='Sales'"
    assert twb.count(anchor) == 1
    twb = twb.replace(anchor, "      </connection>\n" + _EXTRACT + "      <column caption='Sales'")
    package = tmp_path / "superstore.twbx"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("superstore.twb", twb)
        archive.write(hyper_file, "Data/Extracts/orders.hyper")
    return package


def _partition(out: Path) -> str:
    return next(out.glob("*.SemanticModel/definition/tables/Superstore.tmdl")).read_text(encoding="utf-8")


_PACKAGED = MigrationContext(run_output_validation=False, use_packaged_data=True)


def test_by_default_the_workbooks_own_connection_is_kept(tmp_path):
    # A migration keeps the live source: a snapshot of the data would never refresh.
    package = _packaged_superstore(tmp_path)

    result = migrate(package, tmp_path / "out", MigrationContext(run_output_validation=False))

    assert "/data/orders.csv" in _partition(tmp_path / "out")
    assert not (tmp_path / "out" / "data").exists()
    assert any("carries its data as a Tableau extract" in i.message and "--use-packaged-data" in i.message
               for i in result.report.issues)


def test_a_packaged_extract_is_unpacked_and_loaded(tmp_path):
    package = _packaged_superstore(tmp_path)
    out = tmp_path / "out"

    result = migrate(package, out, _PACKAGED)

    csv = out / "data" / "Superstore__Extract.csv"
    lines = csv.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "Sales,Category,Order Date" and len(lines) == 4
    assert "2024-01-31" in lines[1]  # dates as ISO, which Power Query reads in any locale
    partition = _partition(out)
    assert str(csv.resolve()) in partition and "/data/orders.csv" not in partition  # not the author's path
    messages = [i.message for i in result.report.issues]
    assert any(m.startswith("Unpacked the workbook's extract table 'Extract': 3 rows") for m in messages)
    # A column the extract lacks stays in the model, blank, so its visuals still work.
    assert any("left blank: Quantity" in m for m in messages)
    assert 'Table.AddColumn(Typed, "Quantity", each null, Int64.Type)' in partition
    assert "/data/" in (out / ".gitignore").read_text(encoding="utf-8")  # the data isn't committed by default


def test_without_the_hyper_api_the_workbooks_own_connection_stays(tmp_path, monkeypatch):
    package = _packaged_superstore(tmp_path)
    monkeypatch.setitem(sys.modules, "tableauhyperapi", None)  # makes the import fail

    result = migrate(package, tmp_path / "out", _PACKAGED)

    assert any(i.message.startswith("Data not unpacked") and "Hyper API" in i.message for i in result.report.issues)
    assert "/data/orders.csv" in _partition(tmp_path / "out")
    assert not (tmp_path / "out" / "data").exists()


def test_a_plain_twb_is_never_unpacked(book1_twb, tmp_path):
    # Book1's extract names a temp file on the author's machine; its Excel source is what counts.
    workbook = parse_workbook(load_workbook(book1_twb))
    assert workbook.datasources[0].extract is not None
    result = migrate(book1_twb, tmp_path, MigrationContext(run_output_validation=False, use_packaged_data=True))
    assert not (tmp_path / "data").exists()
    assert not any(i.message.startswith(("Unpacked", "Data not unpacked")) for i in result.report.issues)


def test_a_datasource_that_is_an_extract_keeps_its_table(tmp_path):
    # Published extract-only sources: the datasource connects to the .hyper file directly, and its
    # only relation is '[Extract].[Extract]', which used to be skipped as a shadow copy.
    twb = tmp_path / "x.twb"
    twb.write_text(
        "<?xml version='1.0' encoding='utf-8' ?><workbook><datasources>"
        "<datasource caption='Sales' name='ds'><connection class='federated'><named-connections>"
        "<named-connection name='hyper_0'><connection class='hyper' dbname='Data/Extracts/hyper_0.hyper' /></named-connection>"
        "</named-connections><relation connection='hyper_0' name='Extract' table='[Extract].[Extract]' type='table' />"
        "</connection><column datatype='real' name='[Sales]' role='measure' type='quantitative' /></datasource>"
        "</datasources></workbook>",
        encoding="utf-8",
    )
    ds = parse_workbook(load_workbook(twb)).datasources[0]
    assert [r.name for r in ds.relations] == ["Extract"]
    assert (ds.extract.path, ds.extract.tables) == ("Data/Extracts/hyper_0.hyper", ["Extract"])
