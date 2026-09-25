"""Machine-readable source record layout for the member profile flat file.

The design document gives the layout as a table (file position, column name,
field length, data type, mandatory, key column).  Rather than scattering that
knowledge across the parser, the validator and the DDL, it is declared once
here and everything else is derived from it.  Changing the contract is then a
one-line change instead of a hunt through the codebase.

Two deliberate discrepancies exist between the design document and the sample
file, and both are modelled explicitly rather than silently papered over:

1. The layout table declares 11 columns including ``Post Code`` at position 9,
   but the sample header and detail records carry only 10 fields and no post
   code.  The parser therefore binds fields by *header name*, not by ordinal,
   and reports the difference as schema drift.
2. The layout marks ``Member Name`` as the key column.  A member name is not a
   viable business key for a loyalty programme -- ``Member ID`` is.  Both are
   recorded: ``key_column`` mirrors the document, ``business_key`` reflects the
   key the pipeline actually deduplicates on.
3. The document states one date format for the feed, but the sample date of
   birth ``03051985`` is not a valid ``YYYYMMDD`` value (month 19, day 85).  It
   is ``DDMMYYYY``.  Date format is therefore declared per field rather than
   globally, so the dates that genuinely are ``YYYYMMDD`` (enrollment, last
   flight) are not bent to fit the odd one out.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

RECORD_SEPARATOR = "|"
HEADER_TAG = "H"
DETAIL_TAG = "D"


class DataType(str, Enum):
    VARCHAR = "VARCHAR"
    CHAR = "CHAR"
    DATE = "DATE"
    INT = "INT"


#: Date format declared by the File Name / Date-Time specification.
SOURCE_DATE_FORMAT = "%Y%m%d"

#: Format the sample date of birth actually uses (``03051985`` = 3 May 1985).
DOB_DATE_FORMAT = "%d%m%Y"


@dataclass(frozen=True)
class FieldSpec:
    """One column of the source contract."""

    position: int
    name: str
    """Canonical snake_case name used throughout the pipeline."""

    header_aliases: tuple[str, ...]
    """Names this column may appear under in the file header record."""

    length: int
    data_type: DataType
    mandatory: bool
    key_column: bool
    """As declared in the design document."""

    business_key: bool = False
    """Used by the pipeline for deduplication / latest-record-wins."""

    date_format: str = SOURCE_DATE_FORMAT
    """``strptime`` format for DATE columns."""

    alternate_date_formats: tuple[str, ...] = ()
    """Formats tried if ``date_format`` fails, each one reported as drift."""

    def matches_header(self, header_name: str) -> bool:
        normalised = _normalise_header(header_name)
        return normalised in {_normalise_header(a) for a in self.header_aliases}


def _normalise_header(value: str) -> str:
    return value.strip().lower().replace(" ", "_")


#: The source contract, in file position order.
MEMBER_LAYOUT: tuple[FieldSpec, ...] = (
    FieldSpec(1, "member_name", ("Member_Name", "Member Name", "Name"), 255, DataType.VARCHAR, True, True),
    FieldSpec(2, "member_id", ("Member_Id", "Member ID", "Mem_Id"), 18, DataType.VARCHAR, True, False, business_key=True),
    FieldSpec(3, "enrollment_date", ("Enrollment_Date", "Enroll_Dt"), 8, DataType.DATE, True, False),
    FieldSpec(4, "last_flight_date", ("Last_Flight_Date", "Flight_Dt"), 8, DataType.DATE, False, False),
    FieldSpec(5, "tier_code", ("Tier_Code", "TIER"), 5, DataType.CHAR, False, False),
    FieldSpec(6, "agent_name", ("Agent_Name", "Agent Name"), 255, DataType.CHAR, False, False),
    FieldSpec(7, "state", ("State",), 5, DataType.CHAR, False, False),
    # The intermediate table in the design document misspells this as "County".
    # The alias is carried so the misspelling does not break ingestion.
    FieldSpec(8, "country", ("Country", "County"), 5, DataType.CHAR, False, False),
    FieldSpec(9, "post_code", ("Post_Code", "Post Code", "PostCode", "Zip"), 5, DataType.INT, False, False),
    FieldSpec(
        10, "date_of_birth", ("DOB", "Date_Of_Birth", "Date of Birth"), 8,
        DataType.DATE, False, False,
        date_format=DOB_DATE_FORMAT,
        alternate_date_formats=(SOURCE_DATE_FORMAT,),
    ),
    FieldSpec(11, "active_member", ("Is_Active", "Active_Member", "FLAG"), 1, DataType.CHAR, False, False),
)

FIELDS_BY_NAME: dict[str, FieldSpec] = {f.name: f for f in MEMBER_LAYOUT}

MANDATORY_FIELDS: tuple[str, ...] = tuple(f.name for f in MEMBER_LAYOUT if f.mandatory)
DECLARED_KEY_FIELDS: tuple[str, ...] = tuple(f.name for f in MEMBER_LAYOUT if f.key_column)
BUSINESS_KEY_FIELDS: tuple[str, ...] = tuple(f.name for f in MEMBER_LAYOUT if f.business_key)
DATE_FIELDS: tuple[str, ...] = tuple(f.name for f in MEMBER_LAYOUT if f.data_type is DataType.DATE)

#: Filename specification: ``SKYPOINTS_MEMBERS_YYYYMMDD_HHMMSSTT.dat``
MEMBER_FILE_PATTERN = r"^(?P<prefix>[A-Z_]+)_(?P<date>\d{8})_(?P<time>\d{8})\.(?P<ext>dat|txt)$"
REDEMPTION_FILE_PATTERN = r"^(?P<prefix>[A-Z_]+)_(?P<date>\d{8})_(?P<time>\d{8})\.(?P<ext>json|jsonl)$"


def resolve_header(header_names: list[str]) -> tuple[list[FieldSpec | None], list[str]]:
    """Bind the header record of a file to the declared layout.

    Returns the per-position field specs (``None`` where the header column is
    not recognised) together with the names of unrecognised columns.  Binding
    by name rather than ordinal is what makes the pipeline resilient to the
    missing ``Post Code`` column in the sample file.
    """
    bound: list[FieldSpec | None] = []
    unknown: list[str] = []
    for header_name in header_names:
        match = next((f for f in MEMBER_LAYOUT if f.matches_header(header_name)), None)
        bound.append(match)
        if match is None:
            unknown.append(header_name)
    return bound, unknown
