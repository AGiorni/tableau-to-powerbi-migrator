"""Mapping layer: Tableau IR → Power BI IR, plus the migration report."""

from t2pbi.mapping.model_mapper import MappingResult, map_model
from t2pbi.mapping.report import MigrationIssue, MigrationReport

__all__ = ["MappingResult", "MigrationIssue", "MigrationReport", "map_model"]
