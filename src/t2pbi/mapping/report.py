"""Migration report: a structured record of everything that was lossy or needs review."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class IssueSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class MigrationIssue(BaseModel):
    """A single item that could not be migrated cleanly."""

    severity: IssueSeverity
    stage: str = Field(description="Pipeline stage that raised the issue, e.g. 'expression'.")
    object_name: str = Field(description="Tableau object the issue relates to.")
    message: str
    detail: str | None = None


class MigrationReport(BaseModel):
    """Collected issues for one migration run."""

    issues: list[MigrationIssue] = Field(default_factory=list)

    def add(
        self,
        severity: IssueSeverity,
        stage: str,
        object_name: str,
        message: str,
        detail: str | None = None,
    ) -> None:
        self.issues.append(
            MigrationIssue(
                severity=severity,
                stage=stage,
                object_name=object_name,
                message=message,
                detail=detail,
            )
        )

    @property
    def has_errors(self) -> bool:
        return any(i.severity is IssueSeverity.ERROR for i in self.issues)
