"""Derived columns, latest-record-wins and country routing."""

from __future__ import annotations

from datetime import date

import pytest

from skypoints.config import PipelineConfig, conform_country, target_table
from skypoints.models import StagedMember
from skypoints.parser import FlatFileReader
from skypoints.transform import (
    LatestRecordResolver,
    calculate_age,
    is_stale,
    route_by_country,
    stage_member,
    stage_members,
)


# --- age ------------------------------------------------------------------

@pytest.mark.parametrize(
    "dob, as_of, expected",
    [
        (date(1985, 5, 3), date(2024, 1, 15), 38),
        (date(1985, 5, 3), date(2024, 5, 3), 39),    # birthday today counts
        (date(1985, 5, 3), date(2024, 5, 2), 38),    # day before does not
        (date(2004, 2, 29), date(2024, 2, 28), 19),  # leap-day birthday
        (date(2004, 2, 29), date(2024, 3, 1), 20),
        (None, date(2024, 1, 15), None),
    ],
)
def test_age_is_whole_years_at_the_batch_date(dob, as_of, expected):
    assert calculate_age(dob, as_of) == expected


def test_age_is_anchored_on_the_batch_date_not_today():
    # Re-running an old batch must reproduce the values it originally wrote.
    old = calculate_age(date(1985, 5, 3), date(2020, 1, 15))
    assert old == 34


# --- stale flag -----------------------------------------------------------

@pytest.mark.parametrize(
    "last_flight, expected",
    [
        (date(2023, 10, 16), True),    # 91 days
        (date(2023, 10, 17), False),   # exactly 90 days -- the boundary is > 90
        (date(2024, 1, 15), False),
        (None, None),                  # never flown is unknown, not stale
    ],
)
def test_stale_flag_boundary(last_flight, expected):
    assert is_stale(last_flight, date(2024, 1, 15), 90) is expected


def test_never_flown_is_not_conflated_with_stale():
    assert is_stale(None, date(2024, 1, 15), 90) is None


# --- country conformance --------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [("USA", "USA"), ("IND", "IND"), ("PHIL", "PHL"), ("CAN", "CAN"),
     ("AU", "AUS"), ("au", "AUS"), (" IND ", "IND"), ("ATLANTIS", "UNK"),
     (None, "UNK"), ("", "UNK")],
)
def test_country_codes_are_conformed(raw, expected):
    assert conform_country(raw) == expected


def test_alias_and_iso_code_route_to_the_same_table():
    # 'AU' and 'AUS' are the same country; if they routed differently, a source
    # tidying up its codes would look like every Australian member relocating.
    assert target_table(conform_country("AU")) == target_table(conform_country("AUS"))


# --- staging --------------------------------------------------------------

def test_sample_member_is_staged_end_to_end(clean_feed, config):
    reader = FlatFileReader(clean_feed)
    members = list(stage_members(reader.records(), config))
    elena = members[0]

    assert elena.is_valid
    assert elena.member_id == "223457"
    assert elena.date_of_birth == date(1985, 5, 3)   # DDMMYYYY, not YYYYMMDD
    assert elena.age == 38
    assert elena.stale_member is True
    assert elena.target_table == "TABLE_USA"


def test_every_member_in_the_brief_sample_routes_to_its_own_country(clean_feed, config):
    reader = FlatFileReader(clean_feed)
    routed = route_by_country(m for m in stage_members(reader.records(), config) if m.is_valid)

    assert set(routed) == {
        "TABLE_USA", "TABLE_INDIA", "TABLE_PHILIPPINES",
        "TABLE_CANADA", "TABLE_AUSTRALIA",
    }
    assert all(len(rows) == 1 for rows in routed.values())


# --- latest record wins ---------------------------------------------------

def _member(member_id: str, country: str, last_flight: date | None, line: int) -> StagedMember:
    from skypoints.config import conform_country as _cc

    code = _cc(country)
    return StagedMember(
        member_id=member_id,
        country=country,
        country_code=code,
        last_flight_date=last_flight,
        batch_date=date(2024, 1, 15),
        line_number=line,
        target_table=target_table(code),
    )


def test_latest_record_wins_on_most_recent_activity():
    resolver = LatestRecordResolver()
    resolver.add(_member("223457", "USA", date(2024, 1, 1), 2))
    resolver.add(_member("223457", "IND", date(2024, 1, 12), 3))

    winners = list(resolver.winners())
    assert len(winners) == 1
    assert winners[0].country_code == "IND"


def test_resolution_is_order_independent():
    forward, reverse = LatestRecordResolver(), LatestRecordResolver()
    a = _member("223457", "USA", date(2024, 1, 1), 2)
    b = _member("223457", "IND", date(2024, 1, 12), 3)

    forward.add(a)
    forward.add(b)
    reverse.add(b)
    reverse.add(a)

    assert next(forward.winners()).country_code == next(reverse.winners()).country_code


def test_ties_are_broken_deterministically_by_file_position():
    # With no distinguishing dates the later physical row wins, so two runs
    # over the same file cannot disagree.
    resolver = LatestRecordResolver()
    resolver.add(_member("223457", "USA", date(2024, 1, 1), 2))
    resolver.add(_member("223457", "IND", date(2024, 1, 1), 7))

    assert next(resolver.winners()).country_code == "IND"


def test_relocation_reports_the_table_to_delete_from():
    # The row must be removed from the old country table, or the member is
    # counted in two countries at once.
    resolver = LatestRecordResolver()
    resolver.add(_member("223457", "USA", date(2024, 1, 1), 2))
    resolver.add(_member("223457", "IND", date(2024, 1, 12), 3))

    assert resolver.stale_locations() == {"223457": {"TABLE_USA"}}
    assert resolver.stats.country_moves == 1


def test_duplicate_without_a_move_is_not_a_relocation():
    resolver = LatestRecordResolver()
    resolver.add(_member("223457", "USA", date(2024, 1, 1), 2))
    resolver.add(_member("223457", "USA", date(2024, 1, 12), 3))

    assert resolver.stale_locations() == {}
    assert resolver.stats.country_moves == 0
    assert resolver.stats.superseded == 1


def test_distinct_members_are_all_kept():
    resolver = LatestRecordResolver()
    for i in range(5):
        resolver.add(_member(f"2234{i}", "IND", date(2024, 1, 1), i))

    assert len(list(resolver.winners())) == 5
