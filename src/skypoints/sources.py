"""Per-country source contracts.

The three feeds the assessment supplies do not share a schema, a file format,
or a date encoding:

===========  ========  ====================================================
File         Format    Shape
===========  ========  ====================================================
``USA.csv``  CSV       ``ID, Name, TierCode, EnrollmentDate, FlightDate``
                       Dates are ``MDYYYY`` numbers; no date of birth at all.
``IND.csv``  CSV       ``ID, Name, DOB, TierCode, EnrollmentDate,
                       Individual or Corporate, Flight Date``
                       Dates are ``M/D/YYYY``; one column is undeclared.
``AUS.xlsx`` XLSX      ``Unique ID, Member Name, Tier Type, Date of Birth,
                       Date of Enrollment, Date of Flight``
                       Typed datetimes, a literal ``"NULL"``, and an
                       impossible ``2021-13-13``.
===========  ========  ====================================================

Rather than write three readers, the differences are declared as data in
:data:`SOURCE_CONTRACTS` and one reader is driven by them. Onboarding a new
market is then a new contract, not new code -- which is the whole point, given
the brief describes a programme with partners worldwide.

Two decisions worth stating explicitly:

**Country comes from the filename.** No file carries a country column. The
filename is the only thing that identifies the market, so it is treated as part
of the contract. A file whose country cannot be resolved is refused outright
rather than defaulted, because guessing would route real members into the wrong
country's table.

**The business key is (country, member_id).** ``ID`` 1 is Sam in USA, Vikas in
IND and Mike in AUS. If ``member_id`` alone were the key, latest-record-wins
would collapse three different people into one. Qualifying by country is the
only safe reading of this data.

That choice has a known cost, and it is a genuine contradiction in the brief
rather than an oversight here: the assessment asks for "latest record wins when
a member has moved countries", which *requires* an identifier stable across
countries. The supplied data provides no such identifier. Both readings cannot
be satisfied at once, so the pipeline takes the safe one -- never merge distinct
people -- and reports suspected relocations (see
:func:`detect_cross_country_collisions`) for a human to confirm instead of
silently acting on them. If the source can supply a global member ID, the key
becomes that ID and relocation handling works as the brief describes.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .coerce import clean_text, is_null_literal, parse_flexible_date, upper
from .config import PipelineConfig, conform_country, target_table
from .models import Issue, Severity, StagedMember
from .spec import MANDATORY_FIELDS

CSV = "csv"
XLSX = "xlsx"


@dataclass(frozen=True)
class SourceContract:
    """Everything that differs between one country's feed and another's."""

    country_code: str
    file_format: str

    column_map: dict[str, str]
    """Source header (normalised) -> canonical column name."""

    date_formats: tuple[str, ...]
    """``strptime`` formats to try, in order, for this source's dates."""

    encoding: str = "utf-8-sig"
    """``utf-8-sig`` by default: these files carry CRLF endings and may carry a
    byte-order mark, both of which would otherwise corrupt the first header."""

    sheet: str | int = 0

    def canonical(self, header: str) -> str | None:
        return self.column_map.get(_normalise(header))


def _normalise(header: str) -> str:
    return " ".join(str(header).strip().lower().split())


def _snake(header: str) -> str:
    return _normalise(header).replace(" ", "_")


SOURCE_CONTRACTS: dict[str, SourceContract] = {
    "USA": SourceContract(
        country_code="USA",
        file_format=CSV,
        column_map={
            "id": "member_id",
            "name": "member_name",
            "tiercode": "tier_code",
            "enrollmentdate": "enrollment_date",
            "flightdate": "last_flight_date",
        },
        # MDYYYY as a number. Seven-digit values have lost the month's leading
        # zero and are zero-padded before parsing (see parse_flexible_date).
        date_formats=("%m%d%Y",),
    ),
    "IND": SourceContract(
        country_code="IND",
        file_format=CSV,
        column_map={
            "id": "member_id",
            "name": "member_name",
            "dob": "date_of_birth",
            "tiercode": "tier_code",
            "enrollmentdate": "enrollment_date",
            "flight date": "last_flight_date",
        },
        date_formats=("%m/%d/%Y",),
    ),
    "AUS": SourceContract(
        country_code="AUS",
        file_format=XLSX,
        column_map={
            "unique id": "member_id",
            "member name": "member_name",
            "tier type": "tier_code",
            "date of birth": "date_of_birth",
            "date of enrollment": "enrollment_date",
            "date of flight": "last_flight_date",
        },
        # Excel supplies real datetimes; the string formats are for the cells
        # that were typed as text, such as '2021-13-13'.
        date_formats=("%Y-%m-%d", "%d/%m/%Y"),
    ),
}

DATE_COLUMNS = ("enrollment_date", "last_flight_date", "date_of_birth")


def resolve_contract(path: Path | str) -> SourceContract:
    """Identify a file's country from its name.

    Refuses rather than defaults: routing a member into the wrong country's
    table is a worse outcome than failing the file.
    """
    stem = Path(path).stem.strip().upper()
    contract = SOURCE_CONTRACTS.get(stem)
    if contract is None:
        raise LookupError(
            f"no source contract for {Path(path).name!r}; expected one of "
            f"{sorted(SOURCE_CONTRACTS)}. Country is carried by the filename, "
            "so an unrecognised name cannot be routed."
        )
    return contract


# --- readers --------------------------------------------------------------

def _iter_csv_rows(path: Path, contract: SourceContract) -> Iterator[tuple[int, dict]]:
    with path.open("r", encoding=contract.encoding, newline="") as handle:
        reader = csv.DictReader(handle)
        for line_number, row in enumerate(reader, start=2):
            yield line_number, row


def _iter_xlsx_rows(path: Path, contract: SourceContract) -> Iterator[tuple[int, dict]]:
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - environment guard
        raise RuntimeError(
            "reading .xlsx sources requires openpyxl; install with "
            "pip install -e '.[excel]'"
        ) from exc

    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        sheet = (
            workbook[contract.sheet]
            if isinstance(contract.sheet, str)
            else workbook.worksheets[contract.sheet]
        )
        rows = sheet.iter_rows(values_only=True)
        headers = list(next(rows))
        for line_number, values in enumerate(rows, start=2):
            if all(v is None for v in values):
                continue
            yield line_number, dict(zip(headers, values))
    finally:
        workbook.close()


def iter_source(
    path: Path | str, config: PipelineConfig | None = None
) -> Iterator[StagedMember]:
    """Stream one country feed as staged members, defects and all.

    Yields rather than returns so the file is never held in memory. This is
    the entry point the pipeline uses; :func:`read_source` is a convenience
    for tests and interactive work.
    """
    path = Path(path)
    contract = resolve_contract(path)
    config = config or PipelineConfig(as_of_date=date.today())

    rows = (
        _iter_csv_rows(path, contract)
        if contract.file_format == CSV
        else _iter_xlsx_rows(path, contract)
    )
    for line_number, values in rows:
        yield _to_member(values, line_number, path.name, contract, config)


def read_source(
    path: Path | str, config: PipelineConfig | None = None
) -> list[StagedMember]:
    """Collect :func:`iter_source` into a list. Convenience only."""
    return list(iter_source(path, config))


def _to_member(
    values: dict,
    line_number: int,
    source_file: str,
    contract: SourceContract,
    config: PipelineConfig,
) -> StagedMember:
    from .transform import calculate_age, is_stale

    member = StagedMember(
        source_file=source_file,
        line_number=line_number,
        batch_date=config.as_of_date,
        country=contract.country_code,
        country_code=conform_country(contract.country_code),
    )
    member.target_table = target_table(member.country_code, config.target_table_prefix)

    for header, raw in values.items():
        if header is None:
            continue
        column = contract.canonical(header)

        if column is None:
            # Undeclared column: keep it rather than drop it.
            if raw is not None and not is_null_literal(raw):
                member.extras[_snake(header)] = str(raw).strip()
            continue

        if column in DATE_COLUMNS:
            parsed, issue = parse_flexible_date(raw, column, contract.date_formats)
            setattr(member, column, parsed)
            if issue:
                member.issues.append(issue)
        elif column == "tier_code":
            member.tier_code = None if is_null_literal(raw) else upper(str(raw))
        else:
            value = None if is_null_literal(raw) else clean_text(str(raw))
            setattr(member, column, value)

    member.age = calculate_age(member.date_of_birth, config.as_of_date)
    member.stale_member = is_stale(
        member.last_flight_date, config.as_of_date, config.stale_after_days
    )
    member.issues.extend(_validate(member))
    return member


def _validate(member: StagedMember) -> list[Issue]:
    """Contract checks that apply to every source, whatever its shape."""
    issues: list[Issue] = []

    for column in MANDATORY_FIELDS:
        if getattr(member, column, None) in (None, ""):
            issues.append(
                Issue(
                    rule="mandatory_field",
                    severity=Severity.ERROR,
                    message=f"mandatory column {column!r} is missing or unparseable",
                    column=column,
                )
            )

    # A source that declares no DOB is a completeness gap, not a broken record:
    # USA ships none at all, so Age is simply unknowable for that market.
    if member.date_of_birth is None:
        issues.append(
            Issue(
                rule="age_underivable",
                severity=Severity.WARNING,
                message="no date of birth supplied, so Age cannot be derived",
                column="date_of_birth",
            )
        )

    if (
        member.enrollment_date
        and member.last_flight_date
        and member.last_flight_date < member.enrollment_date
    ):
        issues.append(
            Issue(
                rule="flight_after_enrollment",
                severity=Severity.ERROR,
                message=(
                    f"last_flight_date {member.last_flight_date} precedes "
                    f"enrollment_date {member.enrollment_date}"
                ),
                column="last_flight_date",
            )
        )

    return issues


class CrossCountryIdTracker:
    """Detects one ``member_id`` used by more than one country.

    Holds a small set of country codes and names per distinct ID rather than
    the members themselves, so the memory bound is the number of distinct IDs
    rather than the number of rows. That is the difference between state that
    grows with the population and state that grows with the key space -- and it
    is what lets the surrounding pipeline stream.

    It is still per-distinct-ID state, so at hundreds of millions of members
    this belongs in the warehouse (``V_ID_COLLISIONS`` in
    ``sql/08_country_source_ingestion.sql``). This tracker exists to give the
    local run an in-flight signal, not to be the billion-row implementation.
    """

    def __init__(self) -> None:
        self._countries: dict[str, set[str]] = {}
        self._names: dict[str, set[str]] = {}
        self._keys: set[tuple[str, str]] = set()

    def observe(self, member: StagedMember) -> None:
        if not member.member_id:
            return
        self._keys.add(member.member_key)
        self._countries.setdefault(member.member_id, set()).add(member.country_code)
        if member.member_name:
            self._names.setdefault(member.member_id, set()).add(member.member_name)

    def collisions(self) -> dict[str, list[str]]:
        """Member IDs seen in more than one country, with those countries."""
        return {
            member_id: sorted(countries)
            for member_id, countries in self._countries.items()
            if len(countries) > 1
        }

    def names_differ(self, member_id: str) -> bool:
        """Whether one ID carries more than one member name.

        Differing names under a single ID is strong evidence that the ID
        namespaces are not globally stable, so the rows should not be treated
        as one member who relocated. It remains an inference, so it is reported
        for a human to weigh rather than acted on automatically.
        """
        return len(self._names.get(member_id, set())) > 1

    @property
    def distinct_keys(self) -> int:
        return len(self._keys)


def detect_cross_country_collisions(
    members: Iterable[StagedMember],
) -> dict[str, list[StagedMember]]:
    """Eager equivalent of :class:`CrossCountryIdTracker`, for tests.

    Two readings are possible for every hit and the data cannot distinguish
    them: either the member relocated (the brief's "moved countries" case), or
    the two countries number their members independently.

    Reported, never auto-resolved. Merging two people who share an ID is
    unrecoverable; leaving them separate and flagged is not.
    """
    by_id: dict[str, list[StagedMember]] = {}
    for member in members:
        if member.member_id:
            by_id.setdefault(member.member_id, []).append(member)

    return {
        member_id: rows
        for member_id, rows in by_id.items()
        if len({m.country_code for m in rows}) > 1
    }
