"""Ingest layer: turn a Tableau file path into a parsed XML tree."""

from t2pbi.ingest.loader import LoadedWorkbook, load_workbook

__all__ = ["LoadedWorkbook", "load_workbook"]
