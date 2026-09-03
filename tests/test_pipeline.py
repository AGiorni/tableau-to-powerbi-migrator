"""End-to-end test of the deterministic migration pipeline."""

from __future__ import annotations

import json
import re

from x2pbi.graph import run_migration

# Power BI Desktop enforces this exact pattern on the .pbip $schema.
_PBIP_SCHEMA_RE = re.compile(
    r"^https://developer\.microsoft\.com/json-schemas/fabric/pbip/pbipProperties/1\.[0-9]+\.[0-9]+/schema\.json$"
)


def test_run_migration_writes_pbip(superstore_twb, tmp_path):
    result = run_migration(str(superstore_twb), str(tmp_path), chat_model=None)

    pbip = tmp_path / "superstore.pbip"
    assert pbip.exists()
    assert result.pbip_path == pbip

    # PBIP pointer references the report.
    pointer = json.loads(pbip.read_text(encoding="utf-8"))
    assert pointer["artifacts"][0]["report"]["path"] == "superstore.Report"
    # The $schema must satisfy Power BI Desktop's required pattern (Gap 3 regression).
    assert _PBIP_SCHEMA_RE.match(pointer["$schema"])

    # Semantic model definition is present.
    sm = tmp_path / "superstore.SemanticModel" / "definition"
    assert (sm / "database.tmdl").exists()
    assert (sm / "model.tmdl").exists()
    assert (sm / "tables" / "Superstore.tmdl").exists()

    # Report scaffold is present.
    assert (tmp_path / "superstore.Report" / "definition.pbir").exists()
    assert (tmp_path / "superstore.Report" / "definition" / "report.json").exists()
    # PBIR requires a version.json in the report definition folder.
    version = tmp_path / "superstore.Report" / "definition" / "version.json"
    assert version.exists()
    # Desktop requires PBIR file version 2.0.0 to render the report (regression: was 1.0.0).
    assert json.loads(version.read_text(encoding="utf-8"))["version"] == "2.0.0"
    # The base theme resource report.json references must be emitted, or rendering fails.
    theme = (
        tmp_path
        / "superstore.Report"
        / "StaticResources"
        / "SharedResources"
        / "BaseThemes"
        / "Fluent2-CY26SU08.json"
    )
    assert theme.exists()

    # Files are written without a BOM.
    raw = (sm / "model.tmdl").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")


def test_report_flags_untranslated(superstore_twb, tmp_path):
    result = run_migration(str(superstore_twb), str(tmp_path), chat_model=None)

    # Without an LLM, both calculated fields remain untranslated; the LOD one is an error.
    messages = [i.message for i in result.report.issues if i.stage == "expression"]
    assert any("awaiting DAX translation" in m for m in messages)
