"""Mapping layer: source IR → Power BI IR, plus the migration report."""

from x2pbi.mapping.model_mapper import MappingResult, map_model
from x2pbi.mapping.report import MigrationIssue, MigrationReport

__all__ = ["MappingResult", "MigrationIssue", "MigrationReport", "map_model"]
