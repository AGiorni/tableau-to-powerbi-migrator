"""Intermediate Representation (IR) models.

The IR decouples the three stages of migration:

- ``tableau_ir``  : structures parsed from a Tableau workbook.
- ``powerbi_ir``  : structures used to emit a Power BI project.

LLMs and mappers operate on these typed models, never on raw files.
"""

from t2pbi.ir import powerbi_ir, tableau_ir

__all__ = ["powerbi_ir", "tableau_ir"]
