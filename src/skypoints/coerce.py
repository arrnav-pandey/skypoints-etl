"""Type coercion for raw source strings.

Every coercion returns ``(value, issue)`` instead of raising, so a single bad
field degrades one column of one record rather than aborting a batch of
billions.  The issues produced here feed straight into the validation report.
"""

from __future__ import annotations

from datetime import date, datetime

from .models import Issue, Severity
from .spec import SOURCE_DATE_FORMAT


def parse_source_date(raw: str | None, column: str) -> tuple[date | None, Issue | None]:
    """Parse a ``YYYYMMDD`` source date.

    The sample intermediate table shows ``03051985`` landing as ``3051985``:
    the date was handled as a number somewhere upstream and lost its leading
    zero.  An 7-digit all-numeric value is therefore reported explicitly as a
    truncated date rather than as a generic parse failure, because the two have
    very different remediations (fix the upstream cast vs. fix the record).
    """
    if raw is None or raw == "":
        return None, None

    value = raw.strip()
    if len(value) == 7 and value.isdigit():
        return None, Issue(
            rule="date_leading_zero_lost",
            severity=Severity.ERROR,
            message=(
                f"{value!r} is 7 digits; a YYYYMMDD date handled as a number "
                "upstream has lost its leading zero"
            ),
            column=column,
        )

    if len(value) != 8 or not value.isdigit():
        return None, Issue(
            rule="date_format",
            severity=Severity.ERROR,
            message=f"{value!r} is not an 8-digit {SOURCE_DATE_FORMAT} date",
            column=column,
        )

    try:
        return datetime.strptime(value, SOURCE_DATE_FORMAT).date(), None
    except ValueError:
        return None, Issue(
            rule="date_valid_calendar",
            severity=Severity.ERROR,
            message=f"{value!r} is not a valid calendar date",
            column=column,
        )


def parse_int(raw: str | None, column: str) -> tuple[int | None, Issue | None]:
    if raw is None or raw == "":
        return None, None
    value = raw.strip()
    try:
        return int(value), None
    except ValueError:
        return None, Issue(
            rule="integer_format",
            severity=Severity.ERROR,
            message=f"{value!r} is not an integer",
            column=column,
        )


def clean_text(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def upper(raw: str | None) -> str | None:
    value = clean_text(raw)
    return value.upper() if value else None
