"""Core record types shared by the parsing, validation and transform stages."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """Whether a failed rule blocks the record or merely annotates it."""

    ERROR = "ERROR"
    """Record is quarantined and never reaches a target table."""

    WARNING = "WARNING"
    """Record continues, but the issue is surfaced in the run report."""


@dataclass(frozen=True)
class Issue:
    rule: str
    severity: Severity
    message: str
    column: str | None = None

    def __str__(self) -> str:  # pragma: no cover - diagnostics only
        where = f" [{self.column}]" if self.column else ""
        return f"{self.severity.value} {self.rule}{where}: {self.message}"


@dataclass
class RawRecord:
    """A landed detail record, still exactly as the source sent it.

    ``values`` holds raw strings keyed by canonical column name.  Nothing is
    cast or cleaned at this stage: the landing zone is an immutable, faithful
    copy of the source so that any batch can be replayed from it.
    """

    source_file: str
    line_number: int
    record_tag: str
    values: dict[str, str | None]
    raw_line: str
    batch_date: date

    def get(self, column: str) -> str | None:
        return self.values.get(column)


@dataclass
class StagedMember:
    """A member profile after casting, conforming and deriving columns."""

    member_name: str | None = None
    member_id: str | None = None
    enrollment_date: date | None = None
    last_flight_date: date | None = None
    tier_code: str | None = None
    agent_name: str | None = None
    state: str | None = None
    country: str | None = None
    post_code: int | None = None
    date_of_birth: date | None = None
    active_member: str | None = None

    # Derived / lineage columns
    age: int | None = None
    stale_member: bool | None = None
    country_code: str = "UNK"
    target_table: str = ""
    source_file: str = ""
    line_number: int = 0
    batch_date: date | None = None

    issues: list[Issue] = field(default_factory=list)

    extras: dict[str, str] = field(default_factory=dict)
    """Columns a source sends that no specification declares.

    ``IND.csv`` carries ``Individual or Corporate``. Silently dropping a column
    the source chose to send is how real attributes get lost for months, so
    unrecognised columns are retained here rather than discarded.
    """

    @property
    def member_key(self) -> tuple[str, str]:
        """The business key, qualified by country.

        ID 1 is Sam in USA, Vikas in IND and Mike in AUS -- three different
        people sharing one identifier. Keying on ``member_id`` alone would let
        latest-record-wins merge them into a single member, so country forms
        part of the key. See :mod:`skypoints.sources` for what this costs when
        a member genuinely relocates.
        """
        return (self.country_code, self.member_id or "")

    @property
    def is_valid(self) -> bool:
        return not any(i.severity is Severity.ERROR for i in self.issues)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]

    def to_row(self) -> dict[str, Any]:
        """Flatten to a plain dict suitable for a table load or CSV write."""
        return {
            "member_name": self.member_name,
            "member_id": self.member_id,
            "enrollment_date": _iso(self.enrollment_date),
            "last_flight_date": _iso(self.last_flight_date),
            "tier_code": self.tier_code,
            "agent_name": self.agent_name,
            "state": self.state,
            "country": self.country,
            "country_code": self.country_code,
            "post_code": self.post_code,
            "date_of_birth": _iso(self.date_of_birth),
            "active_member": self.active_member,
            "age": self.age,
            "stale_member": self.stale_member,
            "source_file": self.source_file,
            "line_number": self.line_number,
            "batch_date": _iso(self.batch_date),
            "extras": json.dumps(self.extras) if self.extras else None,
        }


@dataclass
class Redemption:
    """One flattened redemption transaction from the partner JSON feed."""

    member_id: str | None = None
    feed_date: date | None = None
    txn_id: str | None = None
    txn_date: date | None = None
    partner: str | None = None
    miles_redeemed: int | None = None
    status: str | None = None
    source_file: str = ""
    record_index: int = 0
    batch_date: date | None = None
    issues: list[Issue] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not any(i.severity is Severity.ERROR for i in self.issues)

    def to_row(self) -> dict[str, Any]:
        return {
            "member_id": self.member_id,
            "feed_date": _iso(self.feed_date),
            "txn_id": self.txn_id,
            "txn_date": _iso(self.txn_date),
            "partner": self.partner,
            "miles_redeemed": self.miles_redeemed,
            "status": self.status,
            "source_file": self.source_file,
            "record_index": self.record_index,
            "batch_date": _iso(self.batch_date),
        }


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None
