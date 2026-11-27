# Book1

`Book1.twb`: Tableau's Superstore sample, six sheets. The main test case — the only sample with
real data, screenshots and a hand-written oracle (`expected.toml`).

**Data.** The workbook reads `sample_-_superstore.xls` from an absolute path in the Downloads
folder of the machine it was made on; the migrated model points at the same path. Rendering in
Power BI Desktop (`scripts/fidelity.py --render`, `--refine`) needs that file there. Structural
checks and the tests don't.

## Screenshots

`screenshots/` holds Tableau's view of each sheet, one folder per sheet. The visual judge
compares the migrated Power BI pages with these. A sheet without a folder isn't compared with a
picture at all; the workbook's embedded 192px thumbnails are not used.

| Tableau sheet | Folder |
|---|---|
| Key Performance Indicators | `Key Performance Indicators/` |
| Sales by Category | `Sales by Category/` |
| Sales by segment | `Sales by segment/` |
| Tooltip: Profit Ratio by City | `Tooltip_ Profit Ratio by City/` (Windows doesn't allow `:`) |
| profit ratio by geography | `profit ratio by geography/` |
| Executive Overview (dashboard) | `Executive Overview/` |

Each folder has `view.png`, the whole sheet. Add more images to a folder when one picture can't
show everything: a close-up of a legend or axis labels, a hovered tooltip. All of them go to the
judge, as views of the same sheet.

How to capture:
- Open the workbook as saved and **don't change filters or selections** — the migration sees the
  saved state, so the reference should too.
- Capture the view with its title, axes and legends. Including Tableau's Columns / Rows / Marks /
  Filters panes is useful: they show how the sheet is built. Windows Snipping Tool
  (`Win + Shift + S`) works.
- PNG or JPEG, at normal screen size; no need to resize.

Book1 uses Tableau's public Superstore sample data, so these images can be committed. Screenshots
of real client workbooks contain client data: keep those out of git.
