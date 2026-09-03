"""Emit layer: Power BI IR → TMDL text and a complete .pbip folder."""

from x2pbi.emit.pbip_assembler import assemble_pbip
from x2pbi.emit.tmdl_writer import render_semantic_model

__all__ = ["assemble_pbip", "render_semantic_model"]
