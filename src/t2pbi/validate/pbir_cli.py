"""Run Microsoft's ``powerbi-report-author`` CLI; ``validate`` is the post-build PBIR gate.

This is the deterministic correctness check for a migrated report — it catches invalid roles,
schema violations, and layout problems before a developer ever opens the file. The CLI is an
optional external dependency (npm ``@microsoft/powerbi-report-authoring-cli``, pinned in
``tools/package.json``); when it is not installed, validation is skipped rather than failing the
migration.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

from t2pbi.mapping.report import IssueSeverity
from t2pbi.toolchain import command, find_node_tool

_CLI = "powerbi-report-author"
# The offline schema fetch failing is an environment artifact, not a defect in the file.
_ENV_ONLY_CODES = {"PBIR_SCHEMA_UNREACHABLE"}


def run_report_author(*cli_args: str, timeout: float = 120.0) -> dict | None:
    """Run ``powerbi-report-author <cli_args>`` and return its parsed JSON ``data`` payload.

    Returns ``None`` when the CLI is not installed, fails to run, or prints no JSON object, so
    callers can degrade gracefully. A non-zero exit (e.g. a failed validation) still yields its
    payload.
    """

    exe = find_node_tool(_CLI)
    if exe is None:
        return None

    args = command(exe, *cli_args)
    try:
        # check=False: a validation failure is a non-zero exit but still prints the diagnostics.
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None

    try:
        payload = json.loads(proc.stdout.strip())
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload.get("data", payload)


def run_pbir_validation(pbip_path: str | Path) -> dict | None:
    """Validate a ``.pbip``/``.Report`` path; ``None`` if the validator is unavailable."""

    return run_report_author("validate", str(pbip_path))


def iter_diagnostics(result: dict) -> Iterator[dict]:
    """Each diagnostic item as ``{severity, code, message, file, path}``: ``file`` is the
    offending file's absolute path when the validator gives one, ``path`` the JSON path in it."""

    for severity, code, message, item in _items(result):
        yield {"severity": severity, "code": code, "message": message,
               "file": item.get("file"), "path": item.get("path")}


def iter_findings(result: dict) -> Iterator[tuple[IssueSeverity, str, str]]:
    """Yield ``(severity, code, message)`` for each diagnostic item in a validation payload.

    Unknown severities count as warnings; environment-only codes are downgraded to info.
    """

    for severity, code, message, _item in _items(result):
        yield severity, code, message


def _items(result: dict) -> Iterator[tuple[IssueSeverity, str, str, dict]]:
    diagnostics = result.get("diagnostics") or {}
    for code, entry in diagnostics.items():
        try:
            base = IssueSeverity(entry.get("severity", "warning"))
        except ValueError:
            base = IssueSeverity.WARNING
        severity = IssueSeverity.INFO if code in _ENV_ONLY_CODES else base
        for item in entry.get("items") or [{}]:
            yield severity, code, item.get("message") or code, item
