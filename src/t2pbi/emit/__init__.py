"""Emit layer: Power BI IR → TMDL text and a complete .pbip folder."""

from t2pbi.emit.pbip_assembler import assemble_pbip
from t2pbi.emit.tmdl_writer import render_semantic_model

__all__ = ["assemble_pbip", "render_semantic_model"]
