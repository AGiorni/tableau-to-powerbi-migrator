"""Score how faithfully t2pbi migrates the sample workbooks.

    python scripts/fidelity.py                    # every workbook with samples/expected/<name>.toml
    python scripts/fidelity.py Book1 --llm        # one workbook, using the configured Azure OpenAI

Migrates each workbook through the normal graph (``migrate()``), then scores the emitted project
against its hand-reviewed expectations. Writes ``<out>/fidelity.json`` and prints a per-sheet table.
This is an evaluator, not a second migration runner. Layers: structural today; numeric (DAX) and
visual (screenshots) come later.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from t2pbi.fidelity import load_expectations, read_project, score_structural
from t2pbi.graph import MigrationContext, migrate
from t2pbi.llm import build_chat_model

_ROOT = Path(__file__).resolve().parents[1]
_EXPECTED = _ROOT / "samples" / "expected"
_INPUT = _ROOT / "samples" / "input"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("workbooks", nargs="*", help="Workbook names (default: all with expectations).")
    parser.add_argument("--out", default=str(_ROOT / "out" / "fidelity"), help="Output folder.")
    parser.add_argument("--llm", action="store_true", help="Use the configured LLM (default: none).")
    args = parser.parse_args(argv)

    names = args.workbooks or sorted(p.stem for p in _EXPECTED.glob("*.toml"))
    chat = build_chat_model() if args.llm else None
    if args.llm and chat is None:
        print("--llm given but no Azure OpenAI configured (T2PBI_AZURE_OPENAI_*).", file=sys.stderr)
        return 2

    out_root = Path(args.out)
    report: dict = {}
    regressions = 0
    for name in names:
        expected = load_expectations(_EXPECTED / f"{name}.toml")
        project_dir = out_root / name
        migrate(_INPUT / expected.workbook, project_dir, MigrationContext(chat_model=chat, run_pbir_validation=False))
        results = score_structural(expected, read_project(project_dir))
        report[name] = _summarize(results)
        regressions += sum(r.outcome in ("fail", "fixed") for r in results)
        _print_table(name, results)

    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "fidelity.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nWrote {out_root / 'fidelity.json'}")
    return 1 if regressions else 0


def _summarize(results) -> dict:
    sheets: dict[str, dict] = {}
    for r in results:
        sheet = sheets.setdefault(r.sheet, {"passed": 0, "total": 0, "checks": []})
        sheet["total"] += 1
        sheet["passed"] += r.passed
        sheet["checks"].append(
            {"check": r.check, "outcome": r.outcome, "detail": r.detail, "known_gap": r.known_gap}
        )
    total = len(results)
    passed = sum(r.passed for r in results)
    return {"structural": {"passed": passed, "total": total, "score": round(passed / total, 3) if total else None}, "sheets": sheets}


def _print_table(name: str, results) -> None:
    print(f"\n{name}: structural fidelity")
    print(f"  {'sheet':34} {'passed':>8}  known gaps / problems")
    by_sheet: dict[str, list] = {}
    for r in results:
        by_sheet.setdefault(r.sheet, []).append(r)
    for sheet, rs in by_sheet.items():
        passed = sum(r.passed for r in rs)
        counts = Counter(r.outcome for r in rs)
        flags = ", ".join(f"{n} {k}" for k, n in counts.items() if k != "pass")
        print(f"  {sheet[:34]:34} {passed:>3}/{len(rs):<4}  {flags}")
        for r in rs:
            if r.outcome in ("fail", "fixed"):
                print(f"      {r.outcome.upper()}: {r.check} — {r.detail or r.known_gap}")
    total = len(results)
    passed = sum(r.passed for r in results)
    print(f"  {'TOTAL':34} {passed:>3}/{total:<4}  score {passed / total:.0%}")


if __name__ == "__main__":
    sys.exit(main())
