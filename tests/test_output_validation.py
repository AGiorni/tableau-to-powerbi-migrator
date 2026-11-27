"""Every sample, migrated through the real graph, must pass Microsoft's own checkers.

This is the guard against writer bugs: if a change to the TMDL or PBIR writers produces something
Power BI would reject, the model or report validator reports an error and this test fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from t2pbi.graph import MigrationContext, migrate
from t2pbi.mapping.report import IssueSeverity

_SAMPLES = sorted((Path(__file__).resolve().parents[1] / "samples").glob("*/*.twb"))
_VALIDATOR_STAGES = ("tmdl", "pbir")


@pytest.mark.external("tmdl-validate", "powerbi-report-author")
@pytest.mark.parametrize("workbook", _SAMPLES, ids=lambda p: p.stem)
def test_sample_passes_both_validators(workbook, tmp_path):
    result = migrate(workbook, tmp_path, MigrationContext(run_output_validation=True))

    issues = [i for i in result.report.issues if i.stage in _VALIDATOR_STAGES]
    errors = [f"{i.stage} {i.object_name}: {i.message}" for i in issues if i.severity is IssueSeverity.ERROR]
    assert not errors, "Validator errors:\n" + "\n".join(errors)
    # Both validators actually ran (not skipped) and said so.
    ran = {i.stage for i in issues if i.object_name == "validator" and "succeeded" in i.message}
    assert ran == set(_VALIDATOR_STAGES), [i.message for i in issues]
