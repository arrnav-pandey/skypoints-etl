"""JSON redemption feed flattening."""

from __future__ import annotations

import json
from datetime import date

from skypoints.redemptions import deduplicate, flatten_document, read_redemptions


def test_brief_sample_document_flattens_to_one_row_per_transaction(config):
    document = json.loads(
        """
        {"member_id": "223457", "feed_date": "20240115",
         "redemptions": [
           {"txn_id": "RX10091", "txn_date": "20240110", "partner": "AeroLink",
            "miles_redeemed": 12000, "status": "COMPLETED"},
           {"txn_id": "RX10092", "txn_date": "20240113", "partner": "SkyPoints",
            "miles_redeemed": 5000, "status": "PENDING"}
         ]}
        """
    )
    rows = list(flatten_document(document, "feed.json", date(2024, 1, 15), config))

    assert [r.txn_id for r in rows] == ["RX10091", "RX10092"]
    # Parent keys are pushed down so each row joins on its own.
    assert all(r.member_id == "223457" for r in rows)
    assert all(r.feed_date == date(2024, 1, 15) for r in rows)
    assert rows[0].miles_redeemed == 12000
    assert rows[1].status == "PENDING"


def test_empty_redemption_array_is_surfaced_not_skipped(config):
    # A partner silently sending nothing must not look like healthy throughput.
    document = {"member_id": "223459", "feed_date": "20240115", "redemptions": []}
    rows = list(flatten_document(document, "feed.json", date(2024, 1, 15), config))

    assert len(rows) == 1
    assert not rows[0].is_valid
    assert any(i.rule == "empty_redemption_array" for i in rows[0].issues)


def test_json_lines_feed_is_streamed(redemption_feed, config):
    rows = list(read_redemptions(redemption_feed, config))
    assert len(rows) == 8
    assert {r.member_id for r in rows} == {"223457", "223458", "223459", "223460", "999999"}


def test_whole_file_json_object_is_accepted(tmp_path, config):
    path = tmp_path / "SKYPOINTS_REDEMPTIONS_20240115_10300000.json"
    path.write_text(
        json.dumps(
            {
                "member_id": "223457",
                "feed_date": "20240115",
                "redemptions": [
                    {"txn_id": "RX1", "txn_date": "20240110", "partner": "A",
                     "miles_redeemed": 10, "status": "COMPLETED"}
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    assert [r.txn_id for r in read_redemptions(path, config)] == ["RX1"]


def test_json_array_feed_is_accepted(tmp_path, config):
    path = tmp_path / "SKYPOINTS_REDEMPTIONS_20240115_10300000.json"
    path.write_text(
        json.dumps(
            [
                {"member_id": "1", "feed_date": "20240115",
                 "redemptions": [{"txn_id": "RX1", "txn_date": "20240110",
                                  "partner": "A", "miles_redeemed": 10,
                                  "status": "COMPLETED"}]},
                {"member_id": "2", "feed_date": "20240115",
                 "redemptions": [{"txn_id": "RX2", "txn_date": "20240110",
                                  "partner": "A", "miles_redeemed": 20,
                                  "status": "COMPLETED"}]},
            ]
        ),
        encoding="utf-8",
    )
    assert [r.txn_id for r in read_redemptions(path, config)] == ["RX1", "RX2"]


def test_negative_miles_are_rejected(config):
    document = {
        "member_id": "1", "feed_date": "20240115",
        "redemptions": [{"txn_id": "RX1", "txn_date": "20240110", "partner": "A",
                         "miles_redeemed": -100, "status": "COMPLETED"}],
    }
    row = next(iter(flatten_document(document, "f.json", date(2024, 1, 15), config)))
    assert not row.is_valid
    assert any(i.rule == "miles_non_negative" for i in row.issues)


def test_unknown_status_warns_but_loads(config):
    document = {
        "member_id": "1", "feed_date": "20240115",
        "redemptions": [{"txn_id": "RX1", "txn_date": "20240110", "partner": "A",
                         "miles_redeemed": 10, "status": "SETTLED"}],
    }
    row = next(iter(flatten_document(document, "f.json", date(2024, 1, 15), config)))
    assert row.is_valid
    assert any(i.rule == "status_domain" for i in row.issues)


def test_malformed_transaction_does_not_break_the_document(config):
    document = {
        "member_id": "1", "feed_date": "20240115",
        "redemptions": [
            "not-an-object",
            {"txn_id": "RX2", "txn_date": "20240110", "partner": "A",
             "miles_redeemed": 10, "status": "COMPLETED"},
        ],
    }
    rows = list(flatten_document(document, "f.json", date(2024, 1, 15), config))

    assert len(rows) == 2
    assert not rows[0].is_valid
    assert rows[1].is_valid       # the good sibling still loads


def test_restated_transaction_keeps_the_latest_status(redemption_feed, config):
    # RX10092 arrives PENDING and is later restated COMPLETED in the same feed.
    rows = [r for r in read_redemptions(redemption_feed, config) if r.is_valid]
    deduped = {r.txn_id: r for r in deduplicate(rows)}

    assert deduped["RX10092"].status == "COMPLETED"
    assert len([r for r in deduplicate(rows) if r.txn_id == "RX10092"]) == 1
