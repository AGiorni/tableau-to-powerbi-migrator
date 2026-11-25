"""Fidelity: how faithfully a migrated Power BI project reproduces the Tableau workbook.

Validity (does it open?) is checked by the validators; fidelity asks whether it shows what Tableau
showed. Three layers, each usable on its own: structural (pages, visual types and field bindings,
read from the files — this module set), numeric (values via DAX) and visual (screenshots against
Tableau images). Used to evaluate t2pbi, and later as tools for the refinement agent.
"""

from t2pbi.fidelity.expectations import (
    FieldSpec,
    SheetExpectation,
    WorkbookExpectations,
    load_expectations,
)
from t2pbi.fidelity.project import Project, read_project
from t2pbi.fidelity.structural import CheckResult, score_structural

__all__ = [
    "CheckResult",
    "FieldSpec",
    "Project",
    "SheetExpectation",
    "WorkbookExpectations",
    "load_expectations",
    "read_project",
    "score_structural",
]
