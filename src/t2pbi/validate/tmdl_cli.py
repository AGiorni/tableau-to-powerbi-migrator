"""Post-build check of the semantic model: load the written TMDL with Microsoft's own serializer.

``powerbi-report-author`` validates only the report (PBIR) and ignores the model entirely, and
``validate_model`` checks the in-memory IR before anything is written. This checks the TMDL files
as written, using TOM (``TmdlSerializer``) through the small .NET tool in ``tools/tmdl-validate``.
When the tool isn't built or .NET isn't installed, the check is skipped, never failed.

What it catches: invalid TMDL text (indentation, unknown keywords, bad enum or data-type values,
unquoted names) and inconsistent metadata (duplicate objects, relationships to missing tables or
columns). What it doesn't: DAX and Power Query text, which TOM stores without parsing.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

from t2pbi.mapping.report import IssueSeverity
from t2pbi.toolchain import dotnet_env, find_dotnet_tool

_TOOL = "tmdl-validate"


def run_tmdl_validation(definition_dir: str | Path, timeout: float = 120.0) -> dict | None:
    """Validate a ``*.SemanticModel/definition`` folder. Returns the tool's JSON, or None when the
    tool is unavailable or its output can't be read."""

    prefix = find_dotnet_tool(_TOOL)
    if prefix is None:
        return None
    try:
        proc = subprocess.run(
            [*prefix, str(definition_dir)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,  # exit 1 means "errors found" and still prints the diagnostics
            env=dotnet_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        payload = json.loads(proc.stdout.strip())
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def definition_dir(pbip_path: str | Path) -> Path:
    """The semantic model's ``definition`` folder for a ``<name>.pbip`` written by t2pbi."""

    pbip = Path(pbip_path)
    return pbip.parent / f"{pbip.stem}.SemanticModel" / "definition"


def iter_findings(result: dict) -> Iterator[tuple[IssueSeverity, str, str]]:
    """Yield ``(severity, object, message)`` per diagnostic; object is ``file:line`` when known."""

    for item in result.get("diagnostics") or []:
        try:
            severity = IssueSeverity(item.get("severity", "error"))
        except ValueError:
            severity = IssueSeverity.ERROR
        where = item.get("file") or "model"
        if item.get("line"):
            where = f"{where}:{item['line']}"
        kind = item.get("kind", "tmdl")
        message = " ".join(str(item.get("message", "")).split())  # TOM's messages span lines
        yield severity, where, f"TMDL {kind} error: {message}"
