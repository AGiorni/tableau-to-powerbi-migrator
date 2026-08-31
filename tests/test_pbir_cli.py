"""Tests for the PBIR validation gate's diagnostic mapping (no subprocess)."""

from __future__ import annotations

from t2pbi.validate.pbir_cli import iter_findings


def test_iter_findings_maps_and_downgrades_env_codes():
    result = {
        "result": "failed",
        "errorCount": 1,
        "warningCount": 1,
        "diagnostics": {
            "PBIR_ROLE_UNKNOWN": {"severity": "error", "items": [{"message": 'Unknown role "X"'}]},
            "PBIR_SCHEMA_UNREACHABLE": {"severity": "warning", "items": [{"message": "offline"}]},
        },
    }
    findings = list(iter_findings(result))
    by_code = {code: severity for severity, code, _ in findings}

    assert by_code["PBIR_ROLE_UNKNOWN"] == "error"
    # Environment-only failure is downgraded to info so it never fails a run.
    assert by_code["PBIR_SCHEMA_UNREACHABLE"] == "info"
    assert len(findings) == 2


def test_iter_findings_handles_empty_diagnostics():
    assert list(iter_findings({"result": "succeeded", "diagnostics": {}})) == []
    assert list(iter_findings({})) == []
