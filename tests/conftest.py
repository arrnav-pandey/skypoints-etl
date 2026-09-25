"""Shared fixtures."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from skypoints.config import PipelineConfig

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "data" / "sample"


@pytest.fixture
def config() -> PipelineConfig:
    return PipelineConfig(as_of_date=date(2024, 1, 15))


@pytest.fixture
def clean_feed() -> Path:
    """The member file exactly as it appears in the brief."""
    return SAMPLE_DIR / "SKYPOINTS_MEMBERS_20240115_10300000.dat"


@pytest.fixture
def dirty_feed() -> Path:
    """A member file carrying the defects the brief hints at."""
    return SAMPLE_DIR / "SKYPOINTS_MEMBERS_20240116_10300000.dat"


@pytest.fixture
def redemption_feed() -> Path:
    return SAMPLE_DIR / "SKYPOINTS_REDEMPTIONS_20240115_10300000.json"


@pytest.fixture
def write_feed(tmp_path: Path):
    """Write an ad-hoc member file and return its path."""

    def _write(lines: list[str], name: str = "SKYPOINTS_MEMBERS_20240115_10300000.dat") -> Path:
        path = tmp_path / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    return _write
