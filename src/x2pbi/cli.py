"""Command-line interface for x2pbi."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from x2pbi.graph import run_migration
from x2pbi.llm import build_chat_model
from x2pbi.sources import ADAPTERS, supported_extensions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="x2pbi", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    migrate = sub.add_parser(
        "migrate", help="Migrate a source workbook to a .pbip project."
    )
    migrate.add_argument(
        "input", help=f"Path to a source file ({', '.join(supported_extensions())})."
    )
    migrate.add_argument("--out", default="out", help="Output directory (default: ./out).")
    migrate.add_argument(
        "--source",
        choices=[a.name for a in ADAPTERS],
        help="Source tool to parse as. Inferred from the file extension when omitted.",
    )
    migrate.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip LLM expression translation (deterministic path only).",
    )
    migrate.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip the powerbi-report-author PBIR validation gate after assembly.",
    )
    migrate.add_argument(
        "--report",
        help="Optional path to write the migration report as JSON.",
    )

    sub.add_parser("sources", help="List the source tools this build can migrate from.")

    args = parser.parse_args(argv)

    if args.command == "migrate":
        return _run_migrate(args)
    if args.command == "sources":
        return _run_sources()
    parser.error(f"Unknown command: {args.command}")
    return 2


def _run_sources() -> int:
    for adapter in ADAPTERS:
        print(f"{adapter.name:<10} {adapter.display_name:<10} {', '.join(adapter.extensions)}")
    return 0


def _run_migrate(args: argparse.Namespace) -> int:
    chat_model = None if args.no_llm else build_chat_model()
    try:
        result = run_migration(
            args.input,
            args.out,
            chat_model=chat_model,
            preflight=not args.no_validate,
            source=args.source,
        )
    # An unknown source, a wrong --source for the file, or a bad path is user error, not a bug:
    # report it on one line rather than dumping a traceback. (UnknownSourceError is a ValueError.)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # A registered but unfinished adapter (Spotfire today) gets its own code so scripts can tell
    # "not supported yet" apart from "you typed it wrong".
    except NotImplementedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3

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
