"""Intermediate Representation (IR) models.

The IR decouples the two halves of a migration:

- ``source_ir``  : the neutral shape every source adapter produces (Tableau, Spotfire, ...).
- ``powerbi_ir`` : structures used to emit a Power BI project.

Mappers and LLMs operate on these typed models, never on raw files. ``source_ir`` is the seam
that makes the migrator multi-source: everything downstream of it is shared.
"""

from x2pbi.ir import powerbi_ir, source_ir

__all__ = ["powerbi_ir", "source_ir"]
