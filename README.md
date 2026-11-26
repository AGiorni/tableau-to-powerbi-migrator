# t2pbi — Tableau → Power BI migrator

[![CI](https://github.com/AGiorni/tableau-to-powerbi-migrator/actions/workflows/ci.yml/badge.svg)](https://github.com/AGiorni/tableau-to-powerbi-migrator/actions/workflows/ci.yml)

AI-assisted tool that ingests a machine-readable Tableau workbook (`.twb` / `.twbx`) and
produces a Power BI Project (`.pbip`) folder ready to open in Power BI Desktop.

## Architecture

A **LangGraph state graph** built around two **Intermediate Representations (IRs)**: the workbook
is parsed into a Tableau IR, mapped to a Power BI IR, and only then written out as TMDL + PBIR.
The graph is the only way a migration runs, from the CLI and from tests alike.

```mermaid
graph TD
    START([start]) --> ingest
    ingest --> map_model
    map_model -.->|translate| translate_expressions
    map_model -.->|no_llm| flag_untranslated
    map_model -.->|nothing_to_translate| map_visuals
    translate_expressions --> validate_expressions
    validate_expressions -.->|repair| translate_expressions
    validate_expressions -.->|done| map_visuals
    flag_untranslated --> map_visuals
    map_visuals --> validate_model
    validate_model --> assemble
    assemble -.->|validate| validate_tmdl
    assemble -.->|skip| END([end])
    validate_tmdl --> validate_pbir
    validate_pbir --> END
```

Dotted edges are decisions made by a router at run time. Regenerate the exact diagram from the
code with `python -c "from t2pbi.graph import build_graph; print(build_graph().get_graph().draw_mermaid())"`.

Deterministic code does parsing and generation. LLMs (Azure OpenAI) are used only where the
workbook is genuinely ambiguous: calculated field → DAX translation (with a validate-and-repair
loop) and visual mapping for worksheets no rule matches. With no LLM configured, the graph
takes the deterministic route and still produces an openable project.

## Status

Working end to end on the sample workbooks:

- **Data model** — tables, relationships, typed Power Query for CSV, Excel, SQL Server,
  PostgreSQL and MySQL; calculated fields translated to DAX (LLM) or flagged for review.
- **Worksheets → visuals** — shelf-aware mapping (orientation, time axis, series, small
  multiples), Measure Names/Values sheets as tables, maps, and an LLM router for ambiguous
  sheets, all checked against Power BI's visual catalog.
- **Dashboards → report pages** with slicers.
- **Validation** — after the build, Microsoft's own checkers read the written files: TOM's TMDL
  serializer for the model and `powerbi-report-author validate` for the report; diagnostics land
  in the migration report.
- **Fidelity** — `scripts/fidelity.py` scores how faithfully the samples are migrated, against
  hand-reviewed expectations in `samples/expected/`: structure (in CI), and with `--render` the
  numbers (queried in Power BI Desktop) and the pictures (an LLM compares Desktop screenshots with
  Tableau screenshots in `samples/reference/`).

Open work is in [BACKLOG.md](BACKLOG.md); the reasoning behind past decisions is in
[docs/DEVLOG.md](docs/DEVLOG.md). For a visual map of the pipeline, open
[docs/architecture.html](docs/architecture.html) in a browser.

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env    # then fill in your settings (Azure OpenAI, Power BI Desktop path)
pytest
```

Settings and secrets live in `.env` (gitignored); `.env.example` lists them all. Real
environment variables take precedence over the file.

Optional, for validating the written project (each check is skipped if its tool is missing):
Microsoft's pinned Power BI tools (needs Node.js) and the TMDL validator (needs a .NET 8 or later SDK).

```powershell
cd tools; npm ci; cd ..
dotnet publish tools\tmdl-validate -c Release -o tools\tmdl-validate\bin\publish
```

Run a migration (deterministic path, no LLM calls):

```powershell
t2pbi migrate .\samples\input\Book1.twb --out .\out --no-llm --report .\out\report.json
```
