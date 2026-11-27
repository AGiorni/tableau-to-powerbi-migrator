# Sample workbooks

The Tableau workbooks t2pbi is developed and tested against. One folder per workbook, laid out
the way a migration job's inputs are: the workbook, and optionally the user's screenshots of it.
Development adds a hand-written oracle.

```
samples/
  <name>/
    <name>.twb            the workbook (.twb or .twbx)
    screenshots/          optional: how Tableau shows each sheet
      <sheet name>/       one folder per worksheet or dashboard, named after it
        view.png          one or more images; more for close-ups or a hovered tooltip
    expected.toml         optional: what a faithful migration shows (fidelity oracle)
    README.md             where the data comes from, how the screenshots were taken
```

| Sample | What it's for | Screenshots | Oracle |
|---|---|---|---|
| [Book1](Book1/) | The real test case: Superstore data, six sheets (a KPI text table, two area charts, a map, a viz-in-tooltip sheet, a dashboard). Fidelity, numbers, pictures, refinement. | yes | yes |
| [superstore](superstore/) | Model mapping from a CSV source; its only worksheet has empty shelves. | — | — |
| [sales_sql](sales_sql/) | Model mapping from a SQL Server source (synthetic: `sql.contoso.com`), no worksheets. | — | — |

Who reads what:
- Every `*/*.twb` is migrated by the tests (`tests/test_output_validation.py`) and by
  `scripts/snapshot_outputs.py`.
- Every `*/expected.toml` is scored by `scripts/fidelity.py` and `tests/test_fidelity_structural.py`.
- `screenshots/` is what `t2pbi migrate --refine --reference-images <folder>` and
  `scripts/fidelity.py --render` give the visual judge. A sheet without a folder (or with an
  empty one) isn't compared with a picture. Folder names are matched to sheet names ignoring case,
  spaces and punctuation, so `Tooltip_ Profit Ratio by City` (Windows doesn't allow `:`) is the
  folder of "Tooltip: Profit Ratio by City". The run warns about folders named after no sheet
  and images outside a sheet's folder.

Adding a sample: make `samples/<name>/`, put the workbook in it, and add screenshots and an
`expected.toml` if it should be scored (format: `src/t2pbi/fidelity/expectations.py`). Commit
only workbooks and screenshots of public or synthetic data: a client's screenshots contain client
data.
