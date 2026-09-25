"""Semantic equivalence between the Python and SQL implementations.

The README claims the SQL is the production path and the Python is the same
logic. That claim is only worth making if something checks it, so the SQL
expressions are re-implemented here exactly as written and compared against the
Python functions across the cases where they could plausibly diverge.

This does not replace running the SQL on Snowflake. It does catch the class of
drift where one implementation is quietly changed and the other is not.
"""

from __future__ import annotations

from calendar import isleap
from datetime import date, timedelta

import pytest

from skypoints.transform import calculate_age, is_stale


def sql_age(dob: date, batch: date) -> int:
    """Transcription of the AGE expression in 04_stage_load.sql / 08.

    ``DATEDIFF('year', dob, batch)
      - IFF(DATE_FROM_PARTS(YEAR(batch), MONTH(dob), DAY(dob)) > batch, 1, 0)``

    Snowflake's ``DATEDIFF('year', ...)`` counts year boundaries crossed, which
    is ``batch.year - dob.year``. ``DATE_FROM_PARTS`` normalises an overflowing
    day, so 29 February in a non-leap year becomes 1 March.
    """
    year_diff = batch.year - dob.year

    if dob.month == 2 and dob.day == 29 and not isleap(batch.year):
        anniversary = date(batch.year, 3, 1)      # DATE_FROM_PARTS normalisation
    else:
        anniversary = date(batch.year, dob.month, dob.day)

    return year_diff - (1 if anniversary > batch else 0)


def sql_stale(last_flight: date, batch: date, threshold: int = 90) -> bool:
    """``DATEDIFF('day', last_flight, batch) > threshold``."""
    return (batch - last_flight).days > threshold


@pytest.mark.parametrize(
    "dob, batch",
    [
        (date(1998, 12, 1), date(2022, 12, 31)),   # IND: Vikas
        (date(1982, 8, 13), date(2022, 12, 31)),   # IND: Rahul
        (date(1952, 8, 13), date(2022, 12, 31)),   # IND: Sameer
        (date(1997, 12, 13), date(2022, 12, 31)),  # AUS: Jonnathan
        (date(1998, 3, 12), date(2022, 12, 31)),   # AUS: Cristina
        (date(1985, 5, 3), date(2024, 5, 2)),      # day before a birthday
        (date(1985, 5, 3), date(2024, 5, 3)),      # on the birthday
        (date(1985, 5, 3), date(2024, 5, 4)),      # day after
        (date(2000, 1, 1), date(2000, 12, 31)),    # first year of life
        (date(2004, 2, 29), date(2024, 2, 28)),    # leap birthday, before
        (date(2004, 2, 29), date(2024, 2, 29)),    # leap birthday, on
        (date(2004, 2, 29), date(2023, 2, 28)),    # leap birthday, non-leap year
        (date(2004, 2, 29), date(2023, 3, 1)),
        (date(1900, 1, 1), date(2026, 1, 1)),
    ],
)
def test_age_agrees_between_python_and_sql(dob, batch):
    assert calculate_age(dob, batch) == sql_age(dob, batch)


def test_age_agrees_across_a_full_year_of_batch_dates():
    # The days/365.25 approximation the SQL previously used drifts from the
    # calendar answer around birthdays. Sweeping a whole year is what makes
    # that class of disagreement impossible to miss.
    dob = date(1990, 6, 15)
    batch = date(2024, 1, 1)
    for _ in range(366):
        assert calculate_age(dob, batch) == sql_age(dob, batch), batch
        batch += timedelta(days=1)


def test_the_old_approximation_really_did_disagree():
    # Guards the reason for the change. On this member's 33rd birthday the
    # days/365.25 approximation returns 32: accumulated leap-year drift leaves
    # the day count just short of 33 * 365.25. Being a year wrong *on someone's
    # birthday* is the worst possible day to be wrong.
    #
    # Kept as a test so nobody "simplifies" the calendar arithmetic back.
    dob, batch = date(1990, 6, 15), date(2023, 6, 15)
    approximate = int((batch - dob).days // 365.25)

    assert calculate_age(dob, batch) == 33
    assert sql_age(dob, batch) == 33
    assert approximate == 32


@pytest.mark.parametrize(
    "last_flight, batch",
    [
        (date(2023, 10, 16), date(2024, 1, 15)),   # 91 days
        (date(2023, 10, 17), date(2024, 1, 15)),   # exactly 90
        (date(2024, 1, 15), date(2024, 1, 15)),    # same day
        (date(2022, 8, 20), date(2022, 12, 31)),   # USA: Sam
    ],
)
def test_stale_flag_agrees_between_python_and_sql(last_flight, batch):
    assert is_stale(last_flight, batch, 90) == sql_stale(last_flight, batch, 90)
