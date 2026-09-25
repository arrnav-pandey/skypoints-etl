"""Type coercion for raw source strings.

Every coercion returns ``(value, issue)`` instead of raising, so a single bad
field degrades one column of one record rather than aborting a batch of
billions.  The issues produced here feed straight into the validation report.
"""

from __future__ import annotations

from datetime import date, datetime

from .models import Issue, Severity
from .spec import SOURCE_DATE_FORMAT


def parse_source_date(
    raw: str | None,
    column: str,
    date_format: str = SOURCE_DATE_FORMAT,
    alternate_formats: tuple[str, ...] = (),
) -> tuple[date | None, Issue | None]:
    """Parse an 8-character source date in ``date_format``.

    The sample intermediate table shows ``03051985`` landing as ``3051985``:
    the date was handled as a number somewhere upstream and lost its leading
    zero.  A 7-digit all-numeric value is therefore reported explicitly as a
    truncated date rather than as a generic parse failure, because the two have
    very different remediations (fix the upstream cast vs. fix the record).

    The format is passed in per column because the feed is not internally
    consistent -- see the discrepancy note in :mod:`skypoints.spec`.  Where a
    column is known to arrive in more than one format, the alternates are tried
    in order and a successful fallback raises a WARNING: the value is usable,
    but the source is drifting from its contract and someone should know.
    """
    if raw is None or raw == "":
        return None, None

    value = raw.strip()
    if len(value) == 7 and value.isdigit():
        return None, Issue(
            rule="date_leading_zero_lost",
            severity=Severity.ERROR,
            message=(
                f"{value!r} is 7 digits; an 8-digit date handled as a number "
                "upstream has lost its leading zero"
            ),
            column=column,
        )

    if len(value) != 8 or not value.isdigit():
        return None, Issue(
            rule="date_format",
            severity=Severity.ERROR,
            message=f"{value!r} is not an 8-digit {date_format} date",
            column=column,
        )

    try:
        return datetime.strptime(value, date_format).date(), None
    except ValueError:
        pass

    for alternate in alternate_formats:
        try:
            parsed = datetime.strptime(value, alternate).date()
        except ValueError:
            continue
        return parsed, Issue(
            rule="date_format_drift",
            severity=Severity.WARNING,
            message=(
                f"{value!r} is not a valid {date_format} date but parses as "
                f"{alternate}; the source is inconsistent with its contract"
            ),
            column=column,
        )

    return None, Issue(
        rule="date_valid_calendar",
        severity=Severity.ERROR,
        message=f"{value!r} is not a valid date in {date_format} or {alternate_formats}",
        column=column,
    )


#: Strings that a source uses to *mean* null but that arrive as text. The AUS
#: workbook sends the four characters N-U-L-L in a date cell; loaded naively it
#: becomes the string 'NULL', which is not null and defeats every IS NULL check
#: downstream. Matching is case-insensitive because sources are not consistent.
NULL_LITERALS: frozenset[str] = frozenset({"null", "n/a", "na", "none", "nil", "-", ""})


def is_null_literal(raw: object) -> bool:
    return isinstance(raw, str) and raw.strip().lower() in NULL_LITERALS


def parse_flexible_date(
    raw: object,
    column: str,
    formats: tuple[str, ...],
) -> tuple[date | None, Issue | None]:
    """Parse a date that may arrive in several shapes within one column.

    The supplied country feeds each encode dates differently, and one of them
    varies *within* a single column:

    * ``AUS.xlsx``  - already a real ``datetime``, because Excel typed it.
    * ``IND.csv``   - ``M/D/YYYY`` text.
    * ``USA.csv``   - ``MDYYYY`` written as a number, so ``6152022`` is 15 June
      2022 with the month's leading zero dropped, while ``12282021`` in the
      same column keeps all eight digits.

    That last case is the important one. Treating a 7-digit date as corrupt
    would quarantine most of the USA file, when in fact the value is perfectly
    recoverable: an 8-character fixed-width date that lost a leading zero is
    zero-padded back to width before parsing.
    """
    if raw is None or is_null_literal(raw):
        return None, None

    # Excel hands back real datetimes; trust the type rather than re-parsing.
    if isinstance(raw, datetime):
        return raw.date(), None
    if isinstance(raw, date):
        return raw, None

    value = str(raw).strip()
    if not value:
        return None, None

    candidates = [value]
    if value.isdigit() and len(value) == 7:
        # A 7-digit value in an 8-character fixed-width date has lost exactly
        # one leading zero, so restore it -- and use *only* the restored form.
        #
        # Trying the raw value first would be wrong, not merely redundant.
        # strptime matches greedily and does not backtrack when the greedy read
        # is itself valid: '1052022' against %m%d%Y takes %m='10', %d='5' and
        # returns 5 October with no error. But 5 October would have been sent
        # as '10052022' -- eight digits -- so seven digits rules that reading
        # out. The field width is the only thing that disambiguates the two.
        candidates = [value.zfill(8)]

    for candidate in candidates:
        for fmt in formats:
            try:
                return datetime.strptime(candidate, fmt).date(), None
            except ValueError:
                continue

    return None, Issue(
        rule="date_valid_calendar",
        severity=Severity.ERROR,
        message=(
            f"{value!r} is not a valid date in any format this source declares "
            f"({', '.join(formats)})"
        ),
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
