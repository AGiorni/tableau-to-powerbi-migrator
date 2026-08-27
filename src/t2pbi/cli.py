"""Command-line interface for t2pbi."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from t2pbi.graph import run_migration
from t2pbi.llm import build_chat_model


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="t2pbi", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    migrate = sub.add_parser("migrate", help="Migrate a Tableau workbook to a .pbip project.")
    migrate.add_argument("input", help="Path to a .twb or .twbx file.")
    migrate.add_argument("--out", default="out", help="Output directory (default: ./out).")
    migrate.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip LLM expression translation (deterministic path only).",
    )
    migrate.add_argument(
        "--report",
        help="Optional path to write the migration report as JSON.",
    )

    args = parser.parse_args(argv)

    if args.command == "migrate":
        return _run_migrate(args)
    parser.error(f"Unknown command: {args.command}")
    return 2


def _run_migrate(args: argparse.Namespace) -> int:
    chat_model = None if args.no_llm else build_chat_model()
    result = run_migration(args.input, args.out, chat_model=chat_model)

    print(f"Wrote project: {result.pbip_path}")
    errors = [i for i in result.report.issues if i.severity.value == "error"]
    warnings = [i for i in result.report.issues if i.severity.value == "warning"]
    print(f"Issues: {len(errors)} error(s), {len(warnings)} warning(s).")

    if args.report:
        Path(args.report).write_text(
            json.dumps(result.report.model_dump(), indent=2), encoding="utf-8"
        )
        print(f"Report written: {args.report}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
