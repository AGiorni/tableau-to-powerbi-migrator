"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "input"


@pytest.fixture
def superstore_twb() -> Path:
    return SAMPLES / "superstore.twb"


@pytest.fixture
def sales_sql_twb() -> Path:
    return SAMPLES / "sales_sql.twb"
