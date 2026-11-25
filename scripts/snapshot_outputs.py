"""Golden-master snapshot of what t2pbi emits, for checking that a change leaves output unchanged.

    python scripts/snapshot_outputs.py snap before.json      # on the old revision
    python scripts/snapshot_outputs.py snap after.json       # on the new revision
    python scripts/snapshot_outputs.py compare before.json after.json

Every sample in ``samples/input`` is migrated three ways: with no LLM; with a fake LLM whose first
DAX answer per formula is broken (forcing the repair loop) and whose visual-router answers are
valid; and with a fake LLM whose router answers are garbage (forcing the fallback). Every emitted
file and every report issue is recorded. Random ids (page/visual folder names, GUIDs) are masked
and issues are compared as a sorted multiset, so two runs of the same code produce identical
snapshots. External validation is off: it measures the environment, not our output.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tempfile
import zlib
from pathlib import Path
from types import SimpleNamespace

from t2pbi.graph import MigrationContext, migrate

_SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "input"
_IDS = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|\b[0-9a-f]{20}\b"
)


class _FakeChat:
    """Deterministic stand-in for the chat model (crc32, not hash(): hash() varies per process)."""

    def __init__(self, router_ok: bool) -> None:
        self._router_ok = router_ok
        self._seen: dict[str, int] = {}

    def invoke(self, messages):
        prompt = messages[-1].content
        if "Tableau formula:\n" not in prompt:  # the visual router's prompt
            if not self._router_ok:
                return SimpleNamespace(content="not json at all")
            names = ast.literal_eval(prompt.split("Available field names: ", 1)[1].strip())
            answer = {"visual_type": "tableEx", "roles": {"Values": names}, "confidence": 0.7}
            return SimpleNamespace(content=json.dumps(answer))
        formula = prompt.split("Tableau formula:\n", 1)[1].split("\n\nYour previous", 1)[0]
        attempt = self._seen.get(formula, 0)
        self._seen[formula] = attempt + 1
        dax = f"SUM('X'[{zlib.crc32(formula.encode()) % 997}])"
        return SimpleNamespace(content=dax[:-1] if attempt == 0 else dax)


def _normalize(out_dir: Path, issues) -> dict:
    files = sorted(
        _IDS.sub("<id>", p.relative_to(out_dir).as_posix())
        + "\n"
        + _IDS.sub("<id>", p.read_text(encoding="utf-8"))
        for p in out_dir.rglob("*")
        if p.is_file()
    )
    return {
        "files": files,
        "issues": sorted(json.dumps(i.model_dump(mode="json"), sort_keys=True) for i in issues),
    }


def snapshot() -> dict:
    runs = {"nollm": lambda: None, "llm": lambda: _FakeChat(True), "badrouter": lambda: _FakeChat(False)}
    snap = {}
    for workbook in sorted(_SAMPLES.glob("*.twb")):
        for label, make_chat in runs.items():
            out = Path(tempfile.mkdtemp())
            context = MigrationContext(chat_model=make_chat(), run_pbir_validation=False)
            result = migrate(workbook, out, context)
            snap[f"{workbook.name}/{label}"] = _normalize(out, result.report.issues)
    return snap


def compare(before: dict, after: dict) -> int:
    differences = 0
    for run in sorted(set(before) | set(after)):
        if run not in before or run not in after:
            print(f"{run}: only in {'after' if run in after else 'before'}")
            differences += 1
            continue
        for part in ("files", "issues"):
            old, new = set(before[run][part]), set(after[run][part])
            for item in sorted(old - new):
                print(f"{run} {part} - {item.splitlines()[0][:150]}")
            for item in sorted(new - old):
                print(f"{run} {part} + {item.splitlines()[0][:150]}")
            differences += len(old ^ new)
    print("IDENTICAL" if differences == 0 else f"{differences} difference(s)")
    return 0 if differences == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    snap_cmd = sub.add_parser("snap", help="Write a snapshot of the current code's output.")
    snap_cmd.add_argument("out")
    cmp_cmd = sub.add_parser("compare", help="Compare two snapshots; exit 1 if they differ.")
    cmp_cmd.add_argument("before")
    cmp_cmd.add_argument("after")
    args = parser.parse_args(argv)

    if args.cmd == "snap":
        snap = snapshot()
        Path(args.out).write_text(json.dumps(snap, indent=1, sort_keys=True), encoding="utf-8")
        print(f"wrote {args.out}: {len(snap)} runs")
        return 0
    before, after = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (args.before, args.after))
    return compare(before, after)


if __name__ == "__main__":
    sys.exit(main())
