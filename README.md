# x2pbi — BI → Power BI migrator

AI-assisted tool that ingests a machine-readable workbook from another BI tool and produces a
Power BI Project (`.pbip`) folder ready to open in Power BI Desktop.

| Source | Extensions | Status |
| --- | --- | --- |
| Tableau | `.twb`, `.twbx` | Supported |
| Spotfire | `.dxp` | Adapter registered; parser not implemented yet |

## Architecture

A **staged pipeline** (LangGraph) built around two intermediate representations. The **source
IR** is the seam that makes the tool multi-source: a source adapter is the only code that knows
a particular BI tool, and everything after the seam is shared.

```
.twb/.twbx ─┐
            ├─► source adapter ──► Source IR ──┐
.dxp ───────┘   (parse only)                   │
                                               ▼
                              model / expression / visual / layout mapping
                                               │
                                               ▼
                                   Power BI IR ──► emit (TMDL + PBIR) ──► .pbip
                                               ▲                            │
                                               └──── validate & repair ◄─────┘
```

Deterministic code does parsing and generation. LLMs (Azure OpenAI) are used surgically for the
hard reasoning: calculated field → DAX/M translation, ambiguous visual mapping, and repair.

## Adding a source

An adapter lives in `src/x2pbi/sources/<tool>/` and satisfies `SourceAdapter`
([sources/base.py](src/x2pbi/sources/base.py)): a `parse(path) -> SourceWorkbook`, plus an
`ExpressionDialect` describing its formula language for the translator. Register it in
`ADAPTERS` ([sources/__init__.py](src/x2pbi/sources/__init__.py)) and nothing else changes.

The adapter normalizes the tool's own vocabulary — connection classes, mark names, aggregation
codes — into the enums in [ir/source_ir.py](src/x2pbi/ir/source_ir.py), keeping the original
string in a `native_*` field for prompts and diagnostics. Unmapped values degrade rather than
fail: an unknown connection kind emits an empty-table stub plus a report warning, and an unknown
mark is routed to the LLM visual router instead of being skipped.

`src/x2pbi/sources/spotfire/parser.py` carries the concept-by-concept mapping still to be filled
in for Spotfire.

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
```

Run a migration (deterministic path — no LLM calls):

```powershell
x2pbi sources                                        # what this build can read
x2pbi migrate .\samples\input\superstore.twb --out .\out --no-llm
```

The source tool is inferred from the file extension; pass `--source tableau` to force it.

## Configuration

LLM translation uses Azure OpenAI, via Entra ID (passwordless) by default:

| Variable | Purpose |
| --- | --- |
| `X2PBI_AZURE_OPENAI_ENDPOINT` | Resource endpoint (required to enable LLM steps) |
| `X2PBI_AZURE_OPENAI_DEPLOYMENT` | Deployment name (default `gpt-4o`) |
| `X2PBI_AZURE_OPENAI_API_VERSION` | API version |
| `X2PBI_AZURE_OPENAI_API_KEY` | Only if key-based auth is required |

The legacy `T2PBI_*` names are still read as a fallback. With nothing configured the pipeline
runs its deterministic path and flags untranslated calculated fields in the migration report.

## Exit codes

`0` success · `2` bad input (unknown extension, wrong `--source`, missing file) · `3` the source
is registered but its parser is not implemented yet.
