"""Command-line interface for t2pbi."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from t2pbi.config import load_env_file
from t2pbi.graph import MigrationContext, migrate
from t2pbi.llm import build_chat_model
from t2pbi.mapping.report import IssueSeverity


def main(argv: list[str] | None = None) -> int:
    # Local settings first (.env), so the external tools started later inherit them too.
    load_env_file()
    parser = argparse.ArgumentParser(prog="t2pbi", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    migrate_cmd = sub.add_parser("migrate", help="Migrate a Tableau workbook to a .pbip project.")
    migrate_cmd.set_defaults(handler=_run_migrate)
    migrate_cmd.add_argument("input", help="Path to a .twb or .twbx file.")
    migrate_cmd.add_argument("--out", default="out", help="Output directory (default: ./out).")
    migrate_cmd.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip LLM expression translation (deterministic path only).",
    )
    migrate_cmd.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip validating the written project (TMDL with TOM, PBIR with powerbi-report-author).",
    )
    migrate_cmd.add_argument(
        "--report",
        help="Optional path to write the migration report as JSON.",
    )
    migrate_cmd.add_argument(
        "--refine",
        action="store_true",
        help="After the first pass, let the refinement agent fix what it got wrong "
        "(needs Azure OpenAI; the project folder becomes a git repository, one commit per fix).",
    )

    args = parser.parse_args(argv)
    return args.handler(args)


def _run_migrate(args: argparse.Namespace) -> int:
    if args.refine and (args.no_llm or args.no_validate):
        print("--refine needs the LLM and validation: drop --no-llm / --no-validate.", file=sys.stderr)
        return 2
    chat_model = None if args.no_llm else build_chat_model()
    if args.refine and chat_model is None:
        print("--refine needs Azure OpenAI (T2PBI_AZURE_OPENAI_* in .env).", file=sys.stderr)
        return 2
    context = MigrationContext(
        chat_model=chat_model,
        run_output_validation=not args.no_validate,
        refine=args.refine,
    )
    result = migrate(args.input, args.out, context)

    print(f"Wrote project: {result.pbip_path}")
    errors = [i for i in result.report.issues if i.severity is IssueSeverity.ERROR]
    warnings = [i for i in result.report.issues if i.severity is IssueSeverity.WARNING]
    print(f"Issues: {len(errors)} error(s), {len(warnings)} warning(s).")

    if args.report:
        Path(args.report).write_text(
            json.dumps(result.report.model_dump(), indent=2), encoding="utf-8"
        )
        print(f"Report written: {args.report}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
