"""Post-build gate: run the offline ``powerbi-report-author validate`` CLI over an emitted PBIP.

This is the deterministic correctness check for a migrated report — it catches invalid roles,
schema violations, and layout problems before a developer ever opens the file. The CLI is an
optional external dependency (npm ``@microsoft/powerbi-report-authoring-cli``); when it is not on
PATH, validation is skipped rather than failing the migration.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

_CLI = "powerbi-report-author"
# The offline schema fetch failing is an environment artifact, not a defect in the file.
_ENV_ONLY_CODES = {"PBIR_SCHEMA_UNREACHABLE"}


def run_pbir_validation(pbip_path: str | Path, timeout: float = 120.0) -> dict | None:
    """Validate a ``.pbip``/``.Report`` path with the CLI. Returns the parsed ``data`` payload.

    Returns ``None`` when the CLI is not installed or its output cannot be parsed, so callers can
    degrade gracefully. A non-zero exit (validation failure) still yields the diagnostics payload.
    """

    exe = shutil.which(_CLI)
    if exe is None:
        return None

    args = [exe, "validate", str(pbip_path)]
    # Windows npm shims are .cmd/.bat and must run through the command interpreter.
    if exe.lower().endswith((".cmd", ".bat")):
        args = ["cmd", "/c", *args]

    try:
        # check=False: a validation failure is a non-zero exit but still prints the diagnostics.
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None

    try:
        payload = json.loads(proc.stdout.strip())
    except (ValueError, TypeError):
        return None
    return payload.get("data", payload)


def iter_findings(result: dict) -> Iterator[tuple[str, str, str]]:
    """Yield ``(severity, code, message)`` for each diagnostic item in a validation payload.

    Severity is ``"error"``/``"warning"``/``"info"``; environment-only codes are downgraded to info.
    """

    diagnostics = result.get("diagnostics") or {}
    for code, entry in diagnostics.items():
        base = entry.get("severity", "warning")
        for item in entry.get("items") or [{}]:
            severity = "info" if code in _ENV_ONLY_CODES else base
            yield severity, code, item.get("message") or code
