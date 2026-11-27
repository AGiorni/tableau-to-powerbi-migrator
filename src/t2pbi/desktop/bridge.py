"""Drive a running Power BI Desktop through Microsoft's Desktop Bridge CLI (``powerbi-desktop``).

The CLI is pinned in ``tools/package.json``. Windows only. Each Desktop window exposes a local
named pipe; the CLI opens a project, reports when Desktop is ready, reloads it from disk and
captures pages as PNGs. One-time machine setup: Desktop signed in (until then a sign-in dialog
blocks the bridge with "Host is not ready").
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from t2pbi.fidelity.references import normalize_name
from t2pbi.toolchain import command, find_node_tool

_CLI = "powerbi-desktop"
# Desktop processes this Python process launched, so they can always be closed (close_all).
_LAUNCHED: set[int] = set()


class BridgeError(RuntimeError):
    """The Desktop Bridge CLI failed or Desktop never became ready."""


def available() -> bool:
    return sys.platform == "win32" and find_node_tool(_CLI) is not None


def desktop_exe() -> Path | None:
    """PBIDesktop.exe: ``$PBI_DESKTOP_PATH`` if it exists, else the Microsoft Store install.

    The bridge CLI finds the classic (MSI) install itself but not the Store one, whose folder name
    changes with every Desktop update — so it is looked up here rather than hard-coded.
    """

    configured = os.getenv("PBI_DESKTOP_PATH")
    if configured and Path(configured).is_file():
        return Path(configured)
    if sys.platform != "win32":
        return None
    try:
        location = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-AppxPackage -Name Microsoft.MicrosoftPowerBIDesktop).InstallLocation"],
            capture_output=True, text=True, timeout=60, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    exe = Path(location) / "bin" / "PBIDesktop.exe" if location else None
    return exe if exe and exe.is_file() else None


def _run(*args: str, timeout: float = 120.0) -> dict:
    exe = find_node_tool(_CLI)
    if exe is None:
        raise BridgeError(f"{_CLI} not installed (npm ci in tools/)")
    env = dict(os.environ)
    found = desktop_exe()
    if found:
        env["PBI_DESKTOP_PATH"] = str(found)
    try:
        proc = subprocess.run(
            command(exe, *args), capture_output=True, text=True, timeout=timeout, check=False, env=env
        )
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(f"{_CLI} {args[0]} timed out after {timeout:.0f}s") from exc
    try:
        payload = json.loads(proc.stdout)
    except ValueError as exc:
        raise BridgeError(f"{_CLI} {args[0]}: unreadable output: {proc.stdout[:300]} {proc.stderr[:300]}") from exc
    if payload.get("status") == "error":
        error = payload.get("error", {})
        raise BridgeError(f"{_CLI} {args[0]}: {error.get('code')}: {error.get('message')}")
    return payload


def open_report(pbip: Path, timeout: float = 180.0) -> int:
    """Launch Desktop on ``pbip``; returns the Desktop process id. Use :func:`wait_ready` next."""

    payload = _run("open", str(pbip.resolve()), "--timeout", str(int(timeout)), timeout=timeout + 30)
    pid = payload.get("pid") or payload.get("launched", {}).get("launchedProcessId")
    if not pid:
        raise BridgeError(f"powerbi-desktop open returned no process id: {payload}")
    _LAUNCHED.add(int(pid))
    return int(pid)


def instance(pid: int) -> dict | None:
    """The bridge's view of one Desktop process, or None if it isn't listed."""

    payload = _run("status")
    return next((i for i in payload.get("instances", []) if i.get("pid") == pid), None)


def wait_ready(pid: int, pbip: Path, timeout: float = 240.0) -> dict:
    """Wait until Desktop ``pid`` has ``pbip`` open and accepts operations; returns its status."""

    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = instance(pid)
        opened = (last or {}).get("currentFilePath") or ""
        if last and last.get("bridgeStatus") == "connected" and Path(opened).resolve() == pbip.resolve():
            return last
        time.sleep(3)
    hint = (last or {}).get("error", {}).get("message", "no status")
    raise BridgeError(
        f"Desktop (pid {pid}) didn't open {pbip.name} within {timeout:.0f}s ({hint}). "
        "If it shows a sign-in dialog, sign in once and retry."
    )


def screenshot_all(pid: int, out_dir: Path, scale: float = 1.0, settle_ms: int = 5000) -> dict[str, Path]:
    """Capture every page as a PNG in ``out_dir``; returns page display name -> file."""

    out_dir.mkdir(parents=True, exist_ok=True)
    pages = (instance(pid) or {}).get("pages", [])
    _run(
        "screenshot-all", "--pid", str(pid), "--output-dir", str(out_dir), "--scale", str(scale),
        "--settle", str(settle_ms), "--wait-seconds", "120", timeout=600,
    )
    files = {normalize_name(p.stem): p for p in out_dir.glob("*.png")}
    shots = {}
    for page in pages:
        name = page.get("displayName", "")
        if normalize_name(name) in files:
            shots[name] = files[normalize_name(name)]
    return shots


def screenshot_page(pid: int, page_id: str, out_path: Path, scale: float = 1.0) -> Path:
    """Capture one page (by its PBIR page id) as a PNG at ``out_path``."""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        "screenshot", page_id, "--pid", str(pid), "--output", str(out_path), "--scale", str(scale),
        "--wait-seconds", "120", timeout=300,
    )
    if not out_path.is_file():
        raise BridgeError(f"no screenshot was written to {out_path}")
    return out_path


def reload(pid: int) -> None:
    """Make Desktop re-read the project from disk (after files changed); loaded data is kept."""

    _run("reload", "--pid", str(pid), "--wait-seconds", "120", timeout=300)


def close(pid: int) -> None:
    """Stop the Desktop process (and its engine) that this run launched."""

    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=60, check=False
    )
    _LAUNCHED.discard(pid)


def close_all() -> None:
    """Close every Desktop this process launched and hasn't closed yet (cleanup after failures)."""

    for pid in list(_LAUNCHED):
        close(pid)
