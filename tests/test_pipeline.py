"""End-to-end batch behaviour."""

from __future__ import annotations

import csv
import json
from datetime import date

from skypoints.config import PipelineConfig
from skypoints.pipeline import run


def _rows(path):
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_clean_batch_lands_every_member_in_its_country(clean_feed, redemption_feed, tmp_path, config):
    report = run(clean_feed, redemption_feed, tmp_path, config)

    assert report.members_read == 5
    assert report.members_quarantined == 0
    assert report.members_loaded == 5
    assert report.rows_per_target == {
        "TABLE_AUSTRALIA": 1, "TABLE_CANADA": 1, "TABLE_INDIA": 1,
        "TABLE_PHILIPPINES": 1, "TABLE_USA": 1,
    }


def test_derived_columns_are_written(clean_feed, tmp_path, config):
    run(clean_feed, None, tmp_path, config)
    row = _rows(tmp_path / "targets" / "table_usa.csv")[0]

    assert row["member_name"] == "Elena"
    assert row["date_of_birth"] == "1985-05-03"
    assert row["age"] == "38"
    assert row["stale_member"] == "True"


def test_rejected_rows_are_quarantined_with_reasons(dirty_feed, tmp_path):
    config = PipelineConfig(as_of_date=date(2024, 1, 16))
    report = run(dirty_feed, None, tmp_path, config)

    assert report.members_quarantined > 0
    quarantined = _rows(tmp_path / "quarantine.csv")
    assert all(row["rejection_reasons"] for row in quarantined)
    assert report.members_read == report.members_loaded + report.members_quarantined + report.members_superseded


def test_relocated_member_appears_in_exactly_one_country(dirty_feed, tmp_path):
    # Elena (223457) arrives twice: USA, then IND with a later flight date.
    config = PipelineConfig(as_of_date=date(2024, 1, 16))
    report = run(dirty_feed, None, tmp_path, config)

    india = _rows(tmp_path / "targets" / "table_india.csv")
    assert [r["member_id"] for r in india] == ["223457"]
    assert not (tmp_path / "targets" / "table_usa.csv").exists()
    assert report.country_moves == 1
    assert report.stale_locations == {"223457": ["TABLE_USA"]}


def test_run_report_is_written_and_machine_readable(clean_feed, redemption_feed, tmp_path, config):
    run(clean_feed, redemption_feed, tmp_path, config)
    report = json.loads((tmp_path / "run_report.json").read_text(encoding="utf-8"))

    assert report["batch_date"] == "2024-01-15"
    assert report["issue_counts"]["WARNING:schema_drift"] == 1
    assert report["notes"]


def test_issue_counts_are_not_double_counted(clean_feed, tmp_path, config):
    run(clean_feed, None, tmp_path, config)
    report = json.loads((tmp_path / "run_report.json").read_text(encoding="utf-8"))

    # Four of the five sample members have no agent name.
    assert report["issue_counts"]["WARNING:optional_completeness"] == 4


def test_batch_is_reproducible(clean_feed, redemption_feed, tmp_path, config):
    # Same inputs and same as-of date must produce byte-identical targets,
    # otherwise no downstream reconciliation can be trusted.
    first = run(clean_feed, redemption_feed, tmp_path / "a", config)
    second = run(clean_feed, redemption_feed, tmp_path / "b", config)

    assert first.to_json() == second.to_json()
    assert (tmp_path / "a" / "targets" / "table_usa.csv").read_bytes() == (
        tmp_path / "b" / "targets" / "table_usa.csv"
    ).read_bytes()


def test_redemptions_are_deduplicated_to_current_state(clean_feed, redemption_feed, tmp_path, config):
    report = run(clean_feed, redemption_feed, tmp_path, config)
    rows = _rows(tmp_path / "targets" / "redemption_txn.csv")

    assert report.redemptions_read == 8
    assert report.redemptions_quarantined == 2      # empty array + negative miles
    assert len({r["txn_id"] for r in rows}) == len(rows)
    assert next(r for r in rows if r["txn_id"] == "RX10092")["status"] == "COMPLETED"


def test_redemptions_join_back_to_members_on_member_id(clean_feed, redemption_feed, tmp_path, config):
    run(clean_feed, redemption_feed, tmp_path, config)

    members = {
        r["member_id"]
        for path in (tmp_path / "targets").glob("table_*.csv")
        for r in _rows(path)
    }
    txns = _rows(tmp_path / "targets" / "redemption_txn.csv")
    orphans = {r["member_id"] for r in txns} - members

    # 223460 and 999999 redeem but are absent from the profile feed -- the
    # out-of-step feed condition the validation suite is meant to surface.
    assert orphans == {"223460", "999999"}
