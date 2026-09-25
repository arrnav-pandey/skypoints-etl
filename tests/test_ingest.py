"""End-to-end ingestion of the supplied country feeds."""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

import pytest

from skypoints.config import PipelineConfig
from skypoints.pipeline import run_sources

INCOMING = Path(__file__).resolve().parents[1] / "data" / "incoming"


@pytest.fixture
def report(tmp_path):
    config = PipelineConfig(as_of_date=date(2022, 12, 31))
    return run_sources(sorted(INCOMING.iterdir()), None, tmp_path, config), tmp_path


def _rows(path):
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_all_three_feeds_are_ingested(report):
    result, _ = report
    assert result.members_read == 9          # three rows per country
    assert result.members_loaded == 8        # AUS Jonnathan is quarantined
    assert result.members_quarantined == 1


def test_each_country_lands_in_its_own_table(report):
    result, out = report

    assert result.rows_per_target == {
        "TABLE_USA": 3, "TABLE_INDIA": 3, "TABLE_AUSTRALIA": 2,
    }
    assert {r["member_name"] for r in _rows(out / "targets" / "table_usa.csv")} == {
        "Sam", "John", "Mike",
    }


def test_colliding_ids_are_reported_not_merged(report):
    # IDs 1, 2 and 3 each appear in all three countries as different people.
    # Nine members must survive as nine, not collapse into three.
    result, _ = report

    assert result.id_collisions
    assert set(result.id_collisions) == {"1", "2", "3"}
    assert result.members_loaded + result.members_quarantined == 9


def test_usa_members_have_no_age(report):
    _, out = report
    for row in _rows(out / "targets" / "table_usa.csv"):
        assert row["age"] == ""


def test_india_members_have_an_age(report):
    _, out = report
    ages = {r["member_name"]: r["age"] for r in _rows(out / "targets" / "table_india.csv")}
    assert ages["Vikas"] == "24"          # born 1998-12-01, as of 2022-12-31


def test_undeclared_column_survives_to_the_target(report):
    _, out = report
    vikas = next(
        r for r in _rows(out / "targets" / "table_india.csv") if r["member_name"] == "Vikas"
    )
    assert json.loads(vikas["extras"])["individual_or_corporate"] == "I"


def test_impossible_date_is_quarantined_with_its_reason(report):
    _, out = report
    quarantined = _rows(out / "quarantine.csv")

    assert len(quarantined) == 1
    assert quarantined[0]["member_name"] == "Jonnathan"
    assert "2021-13-13" in quarantined[0]["rejection_reasons"]


def test_null_literal_did_not_reach_the_target(report):
    # AUS row 1 sends the text 'NULL' as a date of birth. If it survived as a
    # string the target would hold 'NULL' rather than an empty value.
    _, out = report
    mike = next(
        r for r in _rows(out / "targets" / "table_australia.csv")
        if r["member_name"] == "Mike"
    )
    assert mike["date_of_birth"] == ""
    assert mike["age"] == ""


def test_run_is_reproducible(tmp_path):
    config = PipelineConfig(as_of_date=date(2022, 12, 31))
    files = sorted(INCOMING.iterdir())

    first = run_sources(files, None, tmp_path / "a", config)
    second = run_sources(files, None, tmp_path / "b", config)

    assert first.to_json() == second.to_json()
