"""Regenerate the frozen visual catalog from the powerbi-report-author CLI.

Run this once (on a machine with `@microsoft/powerbi-report-authoring-cli` installed) whenever the
targeted Power BI / PBIR version changes:

    python scripts/refresh_visual_catalog.py

It rewrites ``src/x2pbi/mapping/data/visual_catalog.json``, which ``visual_catalog.py`` loads at
import. This is a build-time developer tool; it is never invoked during a migration.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

_CLI = "powerbi-report-author"
_OUT = (
    Path(__file__).resolve().parents[1]
    / "src" / "x2pbi" / "mapping" / "data" / "visual_catalog.json"
)


def _run(*cli_args: str) -> dict | None:
    exe = shutil.which(_CLI)
    if exe is None:
        return None
    argv = [exe, *cli_args]
    if exe.lower().endswith((".cmd", ".bat")):
        argv = ["cmd", "/c", *argv]
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    try:
        return json.loads(proc.stdout.strip()).get("data")
    except (ValueError, AttributeError):
        return None


def main() -> int:
    listing = _run("catalog", "list")
    if listing is None:
        print(
            f"'{_CLI}' not found or failed. "
            "Install with: npm install -g @microsoft/powerbi-report-authoring-cli@latest",
            file=sys.stderr,
        )
        return 1

    visuals: dict[str, dict] = {}
    skipped: list[str] = []
    for vtype in sorted(listing.get("visualTypes", [])):
        described = _run("catalog", "describe", vtype)
        if described is None or "roles" not in described:
            skipped.append(vtype)
            continue
        visuals[vtype] = {
            "roles": {name: spec.get("kind", "Grouping") for name, spec in described["roles"].items()},
            "required": described.get("requiredRoles", []),
            "optional": described.get("optionalRoles", []),
            "max_per_role": described.get("maxPerRole", {}),
        }

    _OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {"_source": "powerbi-report-author catalog describe", "visuals": visuals}
    _OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(visuals)} visual specs to {_OUT}")
    if skipped:
        print(f"Skipped (no data roles): {', '.join(skipped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
