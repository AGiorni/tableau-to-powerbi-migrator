# t2pbi — Tableau → Power BI migrator

AI-assisted tool that ingests a machine-readable Tableau workbook (`.twb` / `.twbx`) and
produces a Power BI Project (`.pbip`) folder ready to open in Power BI Desktop.

## Architecture

A **staged pipeline** (LangGraph) built around a central **Intermediate Representation (IR)**:

```
.twb/.twbx  ──► ingest ──► parse ──► Tableau IR
                                        │
                                        ▼
                              model / expression / visual / layout mapping
                                        │
                                        ▼
                                   Power BI IR ──► emit (TMDL + PBIR) ──► .pbip
                                        ▲                                    │
                                        └────────── validate & repair ◄──────┘
```

Deterministic code does parsing and generation. LLMs (Azure OpenAI) are used surgically for
the hard reasoning: Tableau calc/LOD → DAX/M translation, visual/layout mapping, and repair.

## Status

Working end to end on the sample workbooks:

- **Data model** — tables, relationships, typed Power Query for CSV, Excel, SQL Server,
  PostgreSQL and MySQL; calculated fields translated to DAX (LLM) or flagged for review.
- **Worksheets → visuals** — shelf-aware mapping (orientation, time axis, series, small
  multiples), Measure Names/Values sheets as tables, maps, and an LLM router for ambiguous
  sheets, all checked against Power BI's visual catalog.
- **Dashboards → report pages** with slicers.
- **Validation** — `powerbi-report-author validate` runs after the build; diagnostics land in
  the migration report.

Open work is in [BACKLOG.md](BACKLOG.md); the reasoning behind past decisions is in
[docs/DEVLOG.md](docs/DEVLOG.md). For a visual map of the pipeline, open
[docs/architecture.html](docs/architecture.html) in a browser.

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
```

Run a migration (deterministic path):

```powershell
t2pbi migrate .\samples\input\example.twb --out .\out
```
