"""Assemble a complete ``.pbip`` project folder on disk from a Power BI IR model.

Layout produced (Phase 1 focuses on the semantic model; a minimal report scaffold is emitted
so the project opens in Power BI Desktop):

```
<out>/
├── <name>.pbip
├── .gitignore
├── <name>.SemanticModel/
│   ├── definition.pbism
│   └── definition/
│       ├── database.tmdl
│       ├── model.tmdl
│       ├── relationships.tmdl
│       └── tables/*.tmdl
└── <name>.Report/
    ├── definition.pbir
    └── definition/
        ├── report.json
        └── pages/
            ├── pages.json
            └── <page>/page.json
```

Files are written as UTF-8 **without BOM**, as required for editing PBIP outside Desktop.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from t2pbi.emit.tmdl_writer import render_semantic_model
from t2pbi.ir.powerbi_ir import PBIModel

_SCHEMA_BASE = "https://developer.microsoft.com/json-schemas/fabric"

# The base theme Power BI Desktop bundles into a blank report; it must exist on disk because
# report.json references it via a resourcePackage.
_THEME_NAME = "Fluent2-CY26SU08"
_THEME_FILE = Path(__file__).parent / "resources" / "BaseThemes" / f"{_THEME_NAME}.json"
_THEME_REL = f"StaticResources/SharedResources/BaseThemes/{_THEME_NAME}.json"


def assemble_pbip(model: PBIModel, out_dir: str | Path) -> Path:
    """Write a full ``.pbip`` project for ``model`` under ``out_dir``. Returns the .pbip path."""

    out = Path(out_dir)
    name = model.name
    files = build_file_map(model)

    for rel_path, content in files.items():
        target = out / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        # ``newline=""`` keeps our explicit "\n"s intact; ``encoding`` avoids a BOM.
        target.write_text(content, encoding="utf-8", newline="")

    return out / f"{name}.pbip"


def build_file_map(model: PBIModel) -> dict[str, str]:
    """Return every project file as a ``relative path -> text`` mapping (no disk I/O)."""

    name = model.name
    files: dict[str, str] = {}

    files[f"{name}.pbip"] = _json(_pbip_pointer(name))
    files[".gitignore"] = "**/.pbi/localSettings.json\n**/.pbi/cache.abf\n"

    # Semantic model.
    files[f"{name}.SemanticModel/definition.pbism"] = _json(_pbism())
    for rel, content in render_semantic_model(model).items():
        files[f"{name}.SemanticModel/definition/{rel}"] = content

    # Report scaffold mirroring a Desktop-generated blank PBIR report (expanded in Phase 2).
    page_id = uuid.uuid4().hex[:20]
    files[f"{name}.Report/definition.pbir"] = _json(_pbir(name))
    files[f"{name}.Report/definition/version.json"] = _json(_report_version())
    files[f"{name}.Report/definition/report.json"] = _json(_report())
    files[f"{name}.Report/definition/pages/pages.json"] = _json(_pages(page_id))
    files[f"{name}.Report/definition/pages/{page_id}/page.json"] = _json(_page(page_id))
    files[f"{name}.Report/{_THEME_REL}"] = _read_theme()

    return files


def _pbip_pointer(name: str) -> dict:
    return {
        "$schema": f"{_SCHEMA_BASE}/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0",
        "artifacts": [{"report": {"path": f"{name}.Report"}}],
        "settings": {"enableAutoRecovery": True},
    }


def _pbism() -> dict:
    return {
        "$schema": f"{_SCHEMA_BASE}/item/semanticModel/definitionProperties/1.0.0/schema.json",
        "version": "4.0",
        "settings": {},
    }


def _pbir(name: str) -> dict:
    return {
        "$schema": f"{_SCHEMA_BASE}/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0",
        "datasetReference": {"byPath": {"path": f"../{name}.SemanticModel"}},
    }


def _report_version() -> dict:
    return {
        "$schema": f"{_SCHEMA_BASE}/item/report/definition/versionMetadata/1.0.0/schema.json",
        "version": "2.0.0",
    }


def _report() -> dict:
    return {
        "$schema": f"{_SCHEMA_BASE}/item/report/definition/report/3.3.0/schema.json",
        "themeCollection": {
            "baseTheme": {
                "name": _THEME_NAME,
                "reportVersionAtImport": {"visual": "2.12.0", "report": "3.4.0", "page": "2.3.1"},
                "type": "SharedResources",
            }
        },
        "resourcePackages": [
            {
                "name": "SharedResources",
                "type": "SharedResources",
                "items": [
                    {
                        "name": _THEME_NAME,
                        "path": f"BaseThemes/{_THEME_NAME}.json",
                        "type": "BaseTheme",
                    }
                ],
            }
        ],
        "settings": {
            "useStylableVisualContainerHeader": True,
            "exportDataMode": "AllowSummarized",
            "defaultDrillFilterOtherVisuals": True,
            "allowChangeFilterTypes": True,
            "useEnhancedTooltips": True,
            "useDefaultAggregateDisplayName": True,
        },
    }


def _pages(page_id: str) -> dict:
    return {
        "$schema": f"{_SCHEMA_BASE}/item/report/definition/pagesMetadata/1.1.0/schema.json",
        "pageOrder": [page_id],
        "activePageName": page_id,
    }


def _page(page_id: str) -> dict:
    return {
        "$schema": f"{_SCHEMA_BASE}/item/report/definition/page/2.1.0/schema.json",
        "name": page_id,
        "displayName": "Page 1",
        "displayOption": "FitToPage",
        "height": 1080,
        "width": 1920,
    }


def _read_theme() -> str:
    return _THEME_FILE.read_text(encoding="utf-8")


def _json(obj: dict) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
