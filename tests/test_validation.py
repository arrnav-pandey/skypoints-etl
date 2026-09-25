"""Validation rules, including the defects visible in the brief's sample data."""

from __future__ import annotations

from datetime import date

import pytest

from skypoints.coerce import parse_source_date
from skypoints.models import Severity, StagedMember
from skypoints.parser import FlatFileReader
from skypoints.transform import stage_member, stage_members
from skypoints.validation import UniquenessTracker, validate_member

from helpers import HEADER


def _rules(member: StagedMember) -> set[str]:
    return {i.rule for i in member.issues}


def _stage_one(write_feed, detail: str, config):
    path = write_feed([HEADER, detail])
    record = next(iter(FlatFileReader(path).records()))
    return stage_member(record, config)


# --- the sample data's own defects ---------------------------------------

def test_lost_leading_zero_is_its_own_rule():
    # '03051985' -> '3051985' is an upstream numeric cast, not a bad record.
    # It gets a dedicated rule because the remediation is different.
    value, issue = parse_source_date("3051985", "date_of_birth", "%d%m%Y")
    assert value is None
    assert issue.rule == "date_leading_zero_lost"
    assert issue.severity is Severity.ERROR


def test_dob_is_ddmmyyyy_not_yyyymmdd():
    # '03051985' is 3 May 1985. As YYYYMMDD it would be month 19, day 85.
    value, issue = parse_source_date("03051985", "date_of_birth", "%d%m%Y")
    assert value == date(1985, 5, 3)
    assert issue is None


def test_dob_in_the_other_format_is_accepted_but_flagged():
    value, issue = parse_source_date("19920214", "date_of_birth", "%d%m%Y", ("%Y%m%d",))
    assert value == date(1992, 2, 14)
    assert issue.rule == "date_format_drift"
    assert issue.severity is Severity.WARNING


def test_unmappable_country_blocks_the_record(write_feed, config):
    # An unroutable member would reach no country table at all, so this is an
    # ERROR rather than a cosmetic warning.
    member = _stage_one(
        write_feed, "|D|Mateo|223459|20101012|20121013|GLD|Sam|NCR|MARS|03051985|A", config
    )
    assert not member.is_valid
    assert "country_conformance" in _rules(member)


def test_missing_agent_name_warns_but_loads(clean_feed, config):
    reader = FlatFileReader(clean_feed)
    ravi = list(stage_members(reader.records(), config))[1]

    assert ravi.is_valid
    assert "optional_completeness" in {i.rule for i in ravi.warnings}


def test_over_length_field_is_rejected(write_feed, config):
    # Country is declared CHAR(5); 'ATLANTIS' would be truncated on load and
    # silently become a different country.
    member = _stage_one(
        write_feed, "|D|Mateo|223459|20101012|20121013|GLD|Sam|NCR|ATLANTIS|03051985|A", config
    )
    assert "max_length" in _rules(member)


# --- mandatory fields -----------------------------------------------------

@pytest.mark.parametrize(
    "detail, column",
    [
        ("|D||223457|20101012|20121013|GLD|Sam|CA|USA|03051985|A", "member_name"),
        ("|D|Elena||20101012|20121013|GLD|Sam|CA|USA|03051985|A", "member_id"),
        ("|D|Elena|223457||20121013|GLD|Sam|CA|USA|03051985|A", "enrollment_date"),
    ],
)
def test_mandatory_fields_block_the_record(write_feed, config, detail, column):
    member = _stage_one(write_feed, detail, config)
    assert not member.is_valid
    assert any(i.rule == "mandatory_field" and i.column == column for i in member.errors)


def test_optional_field_may_be_absent(write_feed, config):
    member = _stage_one(
        write_feed, "|D|Elena|223457|20101012||GLD|Sam|CA|USA|03051985|A", config
    )
    assert member.is_valid
    assert member.last_flight_date is None
    assert member.stale_member is None


# --- domain and cross-field ----------------------------------------------

def test_unknown_active_flag_is_rejected(write_feed, config):
    member = _stage_one(
        write_feed, "|D|Elena|223457|20101012|20121013|GLD|Sam|CA|USA|03051985|Z", config
    )
    assert "active_flag_domain" in _rules(member)


def test_unknown_tier_warns_only(write_feed, config):
    # A new tier is a plausible business change; blocking the load over it
    # would stop the pipeline for a marketing decision.
    member = _stage_one(
        write_feed, "|D|Elena|223457|20101012|20121013|XXX|Sam|CA|USA|03051985|A", config
    )
    assert member.is_valid
    assert "tier_code_domain" in {i.rule for i in member.warnings}


def test_flight_before_enrollment_is_rejected(write_feed, config):
    member = _stage_one(
        write_feed, "|D|Elena|223457|20201012|20101013|GLD|Sam|CA|USA|03051985|A", config
    )
    assert "flight_after_enrollment" in _rules(member)


def test_future_date_is_rejected(write_feed, config):
    member = _stage_one(
        write_feed, "|D|Elena|223457|20101012|20991231|GLD|Sam|CA|USA|03051985|A", config
    )
    assert "date_not_future" in _rules(member)


def test_dob_after_enrollment_is_rejected(write_feed, config):
    member = _stage_one(
        write_feed, "|D|Elena|223457|20101012|20121013|GLD|Sam|CA|USA|03052015|A", config
    )
    assert "dob_before_enrollment" in _rules(member)


def test_implausible_birth_year_is_rejected(config):
    member = StagedMember(date_of_birth=date(1850, 1, 1), country_code="USA")
    assert any(i.rule == "dob_plausible" for i in validate_member(member, config))


# --- uniqueness -----------------------------------------------------------

def test_duplicate_business_key_is_detected():
    tracker = UniquenessTracker(["member_id"])
    assert tracker.check(StagedMember(member_id="223457")) is None
    duplicate = tracker.check(StagedMember(member_id="223457"))

    assert duplicate is not None
    assert duplicate.rule == "key_uniqueness"


def test_distinct_keys_are_counted_not_rows():
    tracker = UniquenessTracker(["member_id"])
    for member_id in ("1", "1", "2", "3", "3"):
        tracker.check(StagedMember(member_id=member_id))

    assert tracker.distinct_keys == 3


def test_declared_key_is_not_the_deduplication_key():
    # The document names Member Name as the key column. Two different members
    # can share a name, so deduplicating on it would merge distinct people.
    tracker = UniquenessTracker(["member_name"])
    tracker.check(StagedMember(member_name="Elena", member_id="1"))
    assert tracker.check(StagedMember(member_name="Elena", member_id="2")) is not None
