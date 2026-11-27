# Working in this repo

`t2pbi` migrates Tableau workbooks (`.twb` / `.twbx`) into Power BI Projects (`.pbip`).

## Read these first

- **[docs/architecture.html](docs/architecture.html)** — open in a browser: pipeline stages, the
  worksheet → visual decision order, Tableau → Power BI mapping rules, LLM touchpoints, gotchas.
- **[docs/DEVLOG.md](docs/DEVLOG.md)** — why the code is shaped the way it is, and a reference
  table of format constraints Power BI Desktop enforces. Check it before changing anything in
  `emit/`; several of those constraints look arbitrary and are not.
- **[BACKLOG.md](BACKLOG.md)** — open work and unresolved questions.

## Keeping them current

- Add a **DEVLOG** entry (newest first) when a decision is made or reversed, or when a
  non-obvious constraint is discovered. Not for routine changes — commit messages cover those.
  Record the reasoning and how it was verified, not just the outcome.
- Update **BACKLOG** in the same commit as the work that opens or closes an item. Mark an item
  **(verified)** with the date when it has been confirmed against the code.
- Update **docs/architecture.html** when a change moves what it shows: a stage added or
  reordered, a visual-mapping rule, an LLM touchpoint, a gotcha fixed. Bump the commit and date in
  its footer.
- Propose these updates as part of the change; don't wait to be asked.

## Architecture in one line

`.twb`/`.twbx` → ingest → parse → **Tableau IR** ([ir/tableau_ir.py](src/t2pbi/ir/tableau_ir.py))
→ mapping → **Power BI IR** ([ir/powerbi_ir.py](src/t2pbi/ir/powerbi_ir.py)) → emit (TMDL + PBIR)
→ validate. Orchestrated by one LangGraph `StateGraph` in
[graph/pipeline.py](src/t2pbi/graph/pipeline.py); its topology is drawn in that file's docstring.

## The graph is the only runner

This is an agentic accelerator: validation, repair and LLM steps belong in the graph, where they
are visible, testable and checkpointable. There is exactly one way to run a migration —
`build_graph()` / `migrate()` — used by the CLI and the tests alike. Do not add a second
orchestration path.

- **New stage → new node** in [graph/nodes.py](src/t2pbi/graph/nodes.py). Nodes stay thin: they
  delegate to `ingest/`, `parse/`, `mapping/`, `validate/`, `emit/` and return a partial state
  update. They never mutate the state they were given (copy first, as `validate_expressions` does).
- **New decision, loop or gate → conditional edge** with a named route, not an `if` or retry
  loop inside a node. The DAX repair loop (`validate_expressions` → `repair` →
  `translate_expressions`) is the model to follow.
- **State vs context.** `MigrationState` ([graph/state.py](src/t2pbi/graph/state.py)) holds data
  only, so it stays serializable. Clients, flags and budgets go in `MigrationContext` and are
  read through `runtime.context`.
- **Issues** are returned as `{"issues": [...]}`; the reducer appends them. Mapping functions
  still take a `MigrationReport`; a node creates a fresh one and returns its `issues`.
- `tests/test_graph.py` pins the topology and routing — update it with any graph change.

## Conventions

- Deterministic code parses and generates; LLMs are used **only** where the workbook is genuinely
  ambiguous. If the input already answers the question, read the input. Every LLM path must fall
  back safely to the deterministic result, and the graph must still complete with
  `MigrationContext(chat_model=None)`.
- Unmappable input degrades with a report warning; it does not raise. A partial migration that
  opens beats a clean failure.
- The visual catalog JSON is generated, not hand-edited — refresh it with
  `scripts/refresh_visual_catalog.py`. Visual role names come from the catalog, never intuition.

## Checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts
```

Both must pass. For refactors that should not change output, don't trust the tests alone:
snapshot the output on both revisions and compare —
`python scripts/snapshot_outputs.py snap before.json` (old revision, e.g. via `git worktree`),
the same with `after.json` on the new one, then `python scripts/snapshot_outputs.py compare
before.json after.json`. It masks the random page/visual ids, so it reports only real changes.

For a change to parsing or mapping, also score the local corpus when it's there:
`python scripts/fidelity.py --local` (third-party workbooks in `samples/local/`, gitignored, so
not in every checkout). Book1 alone has hidden general bugs before; a rule must hold across
workbooks, never be fitted to one.

## Settings and secrets

Local settings (Azure OpenAI endpoint and key, `PBI_DESKTOP_PATH`, …) go in `.env` at the repo
root: gitignored, template in `.env.example` (keep the two in sync when adding a setting).
`config.load_env_file()` loads it; real environment variables always win, and an empty value
means "use the default". Never print or commit `.env` values. Tests that read settings must set
`T2PBI_ENV_FILE` to a non-existent file, or they pick up the developer's real `.env`.

## External tools

Microsoft's Power BI tools are pinned in `tools/package.json`; install them with `npm ci` in
`tools/`. The TMDL validator is a small .NET 8 project:
`dotnet publish tools/tmdl-validate -c Release -o tools/tmdl-validate/bin/publish` (set
`DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE=1` first, or the `dotnet` CLI can hang after the build
on a background workload check). `t2pbi/toolchain.py` finds the pinned copies before anything on
PATH, and every caller treats a missing tool as "skip this step". Tests that need a tool are
marked `@pytest.mark.external("<tool>")` and skipped without it; `T2PBI_REQUIRE_TOOLS=1` (set in
CI) makes them fail instead.

CI (`.github/workflows/ci.yml`, GitHub Actions on Windows) installs every tool and runs the
checks above. It runs only when started by hand (`gh workflow run ci.yml`, or the Actions tab):
push and pull-request triggers are off for now, so the local checks are what gate a commit; don't
wait for CI after a push. It can't run anything that needs Power
BI Desktop (screenshots, DAX on data); run those locally. Running against Power BI Desktop needs, once per
machine: Desktop signed in, and for the Store version `PBI_DESKTOP_PATH` set to `PBIDesktop.exe`
inside the Store package folder. The Authoring MCP server only starts if the operator has accepted
its EULA (`PBI_MODELING_MCP_ACCEPT_EULA=true`) — never accept it from code or an agent.

## Parked work

`refactor/source-pluggable` (Spotfire / multi-source refactor) is parked — see BACKLOG.md. Don't
merge it or port its changes unless asked.
