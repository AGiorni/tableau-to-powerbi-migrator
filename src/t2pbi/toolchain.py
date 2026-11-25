"""Locate the external tools t2pbi calls (Microsoft's Power BI CLIs and servers).

The tools are pinned in ``tools/package.json`` and installed with ``npm ci`` in ``tools/``, so a
run uses the same versions everywhere. Lookup order: ``$T2PBI_TOOLS_DIR/node_modules/.bin``, the
repository's ``tools/node_modules/.bin``, then ``PATH``. Every caller treats a missing tool as
"skip this step", never as a failure.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# src/t2pbi/toolchain.py -> repository root (valid for the editable install used in development).
_REPO_TOOLS = Path(__file__).resolve().parents[2] / "tools"


def tools_dir() -> Path:
    """The folder holding the pinned tools: ``$T2PBI_TOOLS_DIR`` if set, else the repo's ``tools/``."""

    override = os.getenv("T2PBI_TOOLS_DIR")
    return Path(override) if override else _REPO_TOOLS


def find_node_tool(name: str) -> str | None:
    """Path to an npm-installed CLI, preferring the pinned copy over one on ``PATH``."""

    bin_dir = tools_dir() / "node_modules" / ".bin"
    for candidate in (bin_dir / f"{name}.cmd", bin_dir / name):
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)


def command(exe: str, *args: str) -> list[str]:
    """The argv to run ``exe``; Windows npm shims (.cmd/.bat) must go through ``cmd /c``."""

    argv = [exe, *args]
    if exe.lower().endswith((".cmd", ".bat")):
        argv = ["cmd", "/c", *argv]
    return argv
