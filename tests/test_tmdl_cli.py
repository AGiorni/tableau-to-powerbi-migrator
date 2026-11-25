"""TMDL validation: the wrapper's parsing (always), and the real tool catching a broken file.

That every sample's model loads cleanly is checked in test_output_validation.py.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from t2pbi.graph import MigrationContext, migrate
from t2pbi.mapping.report import IssueSeverity
from t2pbi.validate.tmdl_cli import definition_dir, iter_findings, run_tmdl_validation


def test_iter_findings_reports_location_and_one_line_messages():
    result = {
        "result": "failed",
        "diagnostics": [
            {"severity": "error", "kind": "format", "file": "./tables/T", "line": 4,
             "message": "TMDL Format Error:\r\n\tParsing error type - Indentation"},
            {"severity": "error", "kind": "metadata", "file": None, "line": None,
             "message": "Cannot resolve all the paths"},
            {"severity": "bogus", "kind": "tool", "message": "x"},
        ],
    }
    assert list(iter_findings(result)) == [
        (IssueSeverity.ERROR, "./tables/T:4", "TMDL format error: TMDL Format Error: Parsing error type - Indentation"),
        (IssueSeverity.ERROR, "model", "TMDL metadata error: Cannot resolve all the paths"),
        (IssueSeverity.ERROR, "model", "TMDL tool error: x"),
    ]
    assert list(iter_findings({"result": "succeeded", "diagnostics": []})) == []


def test_definition_dir_follows_the_pbip_name():
    assert definition_dir(Path("out/Book1.pbip")) == Path("out/Book1.SemanticModel/definition")


@pytest.mark.external("tmdl-validate")
def test_broken_tmdl_is_reported(superstore_twb, tmp_path):
    result = migrate(superstore_twb, tmp_path / "ok", MigrationContext(run_output_validation=False))
    broken = tmp_path / "broken"
    shutil.copytree(definition_dir(result.pbip_path), broken)
    table = broken / "tables" / "Superstore.tmdl"
    table.write_text(table.read_text(encoding="utf-8").replace("\t\tdataType:", "    dataType:", 1), encoding="utf-8")

    findings = list(iter_findings(run_tmdl_validation(broken)))

    assert len(findings) == 1
    severity, where, message = findings[0]
    assert severity is IssueSeverity.ERROR
    assert where.startswith("./tables/Superstore")
    assert "Indentation" in message
