"""Score how faithfully t2pbi migrates the sample workbooks.

    python scripts/fidelity.py                    # structural only: fast, no Desktop, as in CI
    python scripts/fidelity.py Book1 --render     # + numbers and pictures through Power BI Desktop
    python scripts/fidelity.py Book1 --render --llm   # migrate with the LLM too (DAX translation)

Migrates each workbook through the normal graph (``migrate()``), then scores the emitted project:

- structural — pages, visual types, field bindings, against ``samples/expected/<name>.toml``;
- numeric (``--render``) — the visuals' values, queried in Power BI Desktop, against Tableau's
  numbers in the same file;
- visual (``--render``, needs Azure OpenAI) — each page's Desktop screenshot judged by the model
  against the Tableau screenshot in ``samples/reference/<name>/`` (or the embedded thumbnail).

Writes ``<out>/fidelity.json`` and prints the scores. This is an evaluator, not a second migration
runner. Exit code 1 only on structural regressions (the CI-checked layer); numeric and visual
results are reported, not gated.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from t2pbi.config import load_env_file
from t2pbi.fidelity import load_expectations, read_project, score_structural
from t2pbi.fidelity.derived import derive_expectations
from t2pbi.fidelity.references import extract_thumbnails, find_reference_image
from t2pbi.graph import MigrationContext, migrate
from t2pbi.ingest import load_workbook
from t2pbi.llm import build_chat_model
from t2pbi.parse import parse_workbook

_ROOT = Path(__file__).resolve().parents[1]
_EXPECTED = _ROOT / "samples" / "expected"
_INPUT = _ROOT / "samples" / "input"
_REFERENCES = _ROOT / "samples" / "reference"


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("workbooks", nargs="*", help="Workbook names (default: all with expectations).")
    parser.add_argument("--out", default=str(_ROOT / "out" / "fidelity"), help="Output folder.")
    parser.add_argument("--llm", action="store_true", help="Migrate with the configured LLM (default: none).")
    parser.add_argument(
        "--render", action="store_true",
        help="Also check numbers and pictures in Power BI Desktop (Windows; needs the tools and Desktop).",
    )
    parser.add_argument(
        "--derived", action="store_true",
        help="Also score against expectations derived from the workbook itself (what the agent sees).",
    )
    args = parser.parse_args(argv)

    names = args.workbooks or sorted(p.stem for p in _EXPECTED.glob("*.toml"))
    chat = build_chat_model() if (args.llm or args.render) else None
    if args.llm and chat is None:
        print("--llm given but no Azure OpenAI configured (T2PBI_AZURE_OPENAI_*).", file=sys.stderr)
        return 2

    out_root = Path(args.out)
    report: dict = {}
    regressions = 0
    for name in names:
        expected = load_expectations(_EXPECTED / f"{name}.toml")
        project_dir = out_root / name
        result = migrate(
            _INPUT / expected.workbook,
            project_dir,
            MigrationContext(chat_model=chat if args.llm else None, run_output_validation=False),
        )
        project = read_project(project_dir)
        structural = score_structural(expected, project)
        regressions += sum(r.outcome in ("fail", "fixed") for r in structural)
        report[name] = {"structural": _summarize(structural)}
        _print_checks(f"{name}: structural fidelity", structural)

        if args.derived:
            workbook = parse_workbook(load_workbook(_INPUT / expected.workbook))
            derived = score_structural(derive_expectations(workbook, expected.workbook), project)
            report[name]["derived"] = _summarize(derived)
            _print_checks(f"{name}: structural fidelity, derived expectations (no oracle)", derived)

        if args.render:
            numeric, verdicts = asyncio.run(
                _render(name, expected, project, result.pbip_path, out_root / f"{name}-render", chat)
            )
            report[name]["numeric"] = _summarize(numeric)
            report[name]["visual"] = [asdict(v) for v in verdicts]
            _print_checks(f"{name}: numeric fidelity (Desktop)", numeric)
            _print_verdicts(f"{name}: visual fidelity (LLM judge)", verdicts)

    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "fidelity.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nWrote {out_root / 'fidelity.json'}")
    return 1 if regressions else 0


async def _render(name, expected, project, pbip_path, work_dir: Path, chat):
    """Open the project in Desktop: query the numbers, screenshot the pages, then judge them."""

    from t2pbi.desktop.session import desktop_session
    from t2pbi.fidelity.numeric import score_numeric
    from t2pbi.fidelity.visual import VisualVerdict, judge_sheet

    async with desktop_session(Path(pbip_path)) as desktop:
        numeric = await score_numeric(expected, project, desktop.query)
        shots = desktop.screenshot_pages(work_dir / "powerbi")

    thumbnails = extract_thumbnails(_INPUT / expected.workbook, work_dir / "thumbnails")
    verdicts = []
    for sheet in expected.sheets:
        reference = find_reference_image(_REFERENCES / name, sheet.name) or thumbnails.get(sheet.name)
        shot = shots.get(sheet.page_name)
        if chat is None or reference is None or shot is None:
            why = "no Azure OpenAI configured" if chat is None else (
                "no Tableau reference image" if reference is None else "no Power BI page to screenshot"
            )
            verdicts.append(VisualVerdict(sheet=sheet.name, reference="", error=f"skipped: {why}"))
            continue
        verdicts.append(judge_sheet(chat, sheet, reference, shot))
    return numeric, verdicts


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
    return {"passed": passed, "total": total, "score": round(passed / total, 3) if total else None, "sheets": sheets}


def _print_checks(title: str, results) -> None:
    print(f"\n{title}")
    if not results:
        print("  (no checks)")
        return
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
            # Derived checks have no known gaps, so every failure prints: that's the work list.
            if r.outcome in ("fail", "fixed") or (r.outcome == "gap" and "value[" in r.check):
                print(f"      {r.outcome.upper()}: {r.check} — {r.detail or r.known_gap}")
    total = len(results)
    passed = sum(r.passed for r in results)
    print(f"  {'TOTAL':34} {passed:>3}/{total:<4}  score {passed / total:.0%}")


def _print_verdicts(title: str, verdicts) -> None:
    print(f"\n{title}")
    for v in verdicts:
        if v.error:
            print(f"  {v.sheet[:34]:34}   —    {v.error}")
            continue
        mismatches = ", ".join(v.mismatches) or "none"
        print(f"  {v.sheet[:34]:34} {v.score if v.score is not None else '?':>3}/10  mismatches: {mismatches}")
        print(f"      {v.summary}")
    scored = [v.score for v in verdicts if v.score is not None]
    if scored:
        print(f"  {'AVERAGE':34} {sum(scored) / len(scored):>5.1f}/10")


if __name__ == "__main__":
    sys.exit(main())
