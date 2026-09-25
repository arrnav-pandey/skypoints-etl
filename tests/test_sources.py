"""Source contracts for the per-country member feeds.

These tests are written against the files the assessment actually supplied
(``data/incoming/``), not against an idealised feed. Every assertion here
encodes a defect that is genuinely present in that data, so each one is a
statement about the source system rather than about our implementation.

Written before the readers exist: red first, then green.
"""

from __future__ import annotations

import inspect
from datetime import date
from pathlib import Path

import pytest

from skypoints.sources import (
    SOURCE_CONTRACTS,
    CrossCountryIdTracker,
    iter_source,
    read_source,
    resolve_contract,
)

INCOMING = Path(__file__).resolve().parents[1] / "data" / "incoming"


# --- laziness -------------------------------------------------------------

def test_source_reading_is_lazy():
    # The reader must not materialise the file. read_source() is a convenience
    # that collects the stream; iter_source() is the one the pipeline uses, and
    # it has to yield without having read to the end first.
    stream = iter_source(INCOMING / "USA.csv")

    assert inspect.isgenerator(stream)
    assert next(stream).member_name == "Sam"
    stream.close()


def test_xlsx_reading_is_lazy():
    stream = iter_source(INCOMING / "AUS.xlsx")

    assert inspect.isgenerator(stream)
    assert next(stream).member_name == "Mike"
    stream.close()


# --- collision tracking without retaining the population ------------------

def test_collision_tracker_keeps_ids_not_members():
    # Cross-country collisions need per-ID state, not the members themselves.
    # Holding every StagedMember would make the population the memory bound;
    # holding a small set per distinct ID makes distinct-ID count the bound.
    tracker = CrossCountryIdTracker()
    for member in iter_source(INCOMING / "USA.csv"):
        tracker.observe(member)
    for member in iter_source(INCOMING / "IND.csv"):
        tracker.observe(member)

    assert tracker.collisions() == {"1": ["IND", "USA"], "2": ["IND", "USA"], "3": ["IND", "USA"]}
    assert tracker.distinct_keys == 6


def test_collision_tracker_reports_whether_names_differ():
    # Different names under one ID is evidence the ID namespaces are not
    # globally stable, which is why the records are not merged.
    tracker = CrossCountryIdTracker()
    for path in ("USA.csv", "IND.csv"):
        for member in iter_source(INCOMING / path):
            tracker.observe(member)

    assert tracker.names_differ("1") is True


def test_single_country_ids_are_not_collisions():
    tracker = CrossCountryIdTracker()
    for member in iter_source(INCOMING / "USA.csv"):
        tracker.observe(member)

    assert tracker.collisions() == {}


# --- country is carried by the filename, not by a column -------------------

def test_country_is_derived_from_the_filename():
    # None of the three files contains a country column. The only thing that
    # says "these are Australian members" is the name of the file.
    assert resolve_contract(INCOMING / "AUS.xlsx").country_code == "AUS"
    assert resolve_contract(INCOMING / "USA.csv").country_code == "USA"
    assert resolve_contract(INCOMING / "IND.csv").country_code == "IND"


def test_unknown_country_file_is_rejected_rather_than_guessed():
    with pytest.raises(LookupError):
        resolve_contract(Path("MARS.csv"))


def test_every_contract_declares_its_own_format():
    assert {c.file_format for c in SOURCE_CONTRACTS.values()} == {"csv", "xlsx"}


# --- USA: MDYYYY integers that have lost their leading zero ----------------

def test_usa_dates_lose_their_leading_zero_and_must_still_parse():
    # '6152022' is 15 June 2022 written as M-D-YYYY with the leading zero of
    # the month dropped by an upstream numeric cast. Rejecting 7-digit dates
    # would quarantine most of this file, so they must be parsed, not refused.
    members = read_source(INCOMING / "USA.csv")

    sam = members[0]
    assert sam.member_id == "1"
    assert sam.member_name == "Sam"
    assert sam.enrollment_date == date(2022, 6, 15)
    assert sam.last_flight_date == date(2022, 8, 20)


def test_usa_eight_digit_dates_parse_in_the_same_column():
    # '12282021' is 28 Dec 2021 - same column, 8 digits, no zero lost. The
    # parser must handle both widths without being told which to expect.
    mike = read_source(INCOMING / "USA.csv")[2]
    assert mike.enrollment_date == date(2021, 12, 28)
    assert mike.last_flight_date == date(2021, 12, 30)


def test_seven_digit_date_is_padded_not_matched_greedily():
    # '1052022' is 5 January 2022, NOT 5 October 2022.
    #
    # A 7-digit MDYYYY value must have lost exactly one leading zero, so its
    # month is necessarily single-digit: 5 Oct 2022 would have been written
    # '10052022' with all eight digits and never appeared as seven.
    #
    # This matters because strptime matches greedily and does not backtrack
    # when the greedy read happens to be valid: '%m' takes '10', '%d' takes
    # '5', and October is returned with no error raised. The width of the
    # field is the only thing that disambiguates it.
    john = read_source(INCOMING / "USA.csv")[1]

    assert john.enrollment_date == date(2022, 1, 5)
    assert john.last_flight_date == date(2022, 1, 15)


def test_usa_has_no_date_of_birth_so_age_is_unknown():
    # An entire country ships no DOB. Age must be NULL rather than zero or a
    # guess - a fabricated age would silently corrupt every age-based segment.
    for member in read_source(INCOMING / "USA.csv"):
        assert member.date_of_birth is None
        assert member.age is None


# --- IND: slash-delimited M/D/YYYY and an undeclared column ----------------

def test_ind_slash_dates_parse():
    vikas = read_source(INCOMING / "IND.csv")[0]

    assert vikas.member_name == "Vikas"
    assert vikas.date_of_birth == date(1998, 12, 1)
    assert vikas.enrollment_date == date(2022, 1, 1)
    assert vikas.last_flight_date == date(2022, 6, 15)


def test_ind_carries_a_column_no_specification_mentions():
    # 'Individual or Corporate' appears in no design document. It is retained
    # rather than dropped, because silently discarding a column the source
    # chose to send is how real attributes get lost for months.
    vikas = read_source(INCOMING / "IND.csv")[0]
    assert vikas.extras.get("individual_or_corporate") == "I"


# --- AUS: Excel, with typed cells and two traps ---------------------------

def test_aus_excel_datetimes_parse():
    cristina = read_source(INCOMING / "AUS.xlsx")[2]

    assert cristina.member_name == "Cristina"
    assert cristina.date_of_birth == date(1998, 3, 12)
    assert cristina.enrollment_date == date(2022, 3, 12)
    assert cristina.last_flight_date == date(2022, 3, 20)


def test_literal_null_string_becomes_a_real_null():
    # AUS row 1 holds the four characters N-U-L-L, not an empty cell. Loaded
    # naively it becomes the text 'NULL', which is not null and breaks every
    # IS NULL check downstream.
    mike = read_source(INCOMING / "AUS.xlsx")[0]

    assert mike.date_of_birth is None
    assert mike.age is None


def test_impossible_calendar_date_is_rejected_not_coerced():
    # AUS row 2 enrolment is '2021-13-13' - month 13. There is no defensible
    # way to repair this, so the record is quarantined with a reason instead
    # of being silently shifted into 2022.
    jonnathan = read_source(INCOMING / "AUS.xlsx")[1]

    assert jonnathan.enrollment_date is None
    assert not jonnathan.is_valid
    assert any(i.rule == "date_valid_calendar" for i in jonnathan.errors)


def test_valid_columns_survive_an_invalid_one_in_the_same_row():
    # One bad cell must not cost the whole record. The member is quarantined,
    # but the columns that did parse are preserved so the row can be repaired.
    jonnathan = read_source(INCOMING / "AUS.xlsx")[1]

    assert jonnathan.member_name == "Jonnathan"
    assert jonnathan.date_of_birth == date(1997, 12, 13)
    assert jonnathan.last_flight_date == date(2022, 1, 5)


# --- cross-file: the identifier collision ---------------------------------

def test_member_ids_collide_across_countries():
    # ID 1 is Sam in USA, Vikas in IND and Mike in AUS: three different people
    # sharing one identifier. member_id alone therefore cannot be the key.
    first_ids = {
        read_source(INCOMING / name)[0].member_id
        for name in ("USA.csv", "IND.csv", "AUS.xlsx")
    }
    assert first_ids == {"1"}


def test_member_key_is_qualified_by_country():
    # Scoping the key by country is what stops the three ID-1 members being
    # merged into one person by the latest-record-wins rule.
    sam = read_source(INCOMING / "USA.csv")[0]
    vikas = read_source(INCOMING / "IND.csv")[0]

    assert sam.member_key != vikas.member_key
    assert sam.member_key == ("USA", "1")


# --- lineage --------------------------------------------------------------

def test_every_record_knows_where_it_came_from():
    member = read_source(INCOMING / "IND.csv")[1]

    assert member.source_file == "IND.csv"
    assert member.line_number == 3          # header is line 1
    assert member.country_code == "IND"
