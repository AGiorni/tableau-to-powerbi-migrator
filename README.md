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

Phase 0 (scaffolding) + Phase 1 (data-model migration) in progress. See `src/t2pbi/`.

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
