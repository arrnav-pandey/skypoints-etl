"""Staging transformations: cast, conform, derive, deduplicate, route.

Deliverables 2 and 3 of the brief live here:

* ``stage_member``  -- casts the raw record and derives ``age`` and the
  ``stale_member`` flag (days since last flight > 90).
* ``LatestRecordResolver`` -- applies the "latest record wins" rule when a
  member appears more than once in a batch, including when they have moved
  country, so exactly one row per member reaches exactly one target table.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Iterator

from .coerce import clean_text, parse_int, parse_source_date, upper
from .config import PipelineConfig, conform_country, target_table
from .models import Issue, RawRecord, Severity, StagedMember
from .spec import FIELDS_BY_NAME
from .validation import validate_member, validate_record


def calculate_age(date_of_birth: date | None, as_of: date) -> int | None:
    """Whole years between birth and the batch date.

    Computed from the injected batch date rather than ``date.today()`` so a
    re-run of an old batch reproduces the original values exactly.
    """
    if date_of_birth is None:
        return None
    years = as_of.year - date_of_birth.year
    if (as_of.month, as_of.day) < (date_of_birth.month, date_of_birth.day):
        years -= 1
    return years if years >= 0 else None


def is_stale(last_flight_date: date | None, as_of: date, threshold_days: int) -> bool | None:
    """``True`` when days since the last flight exceed the threshold.

    A member who has never flown has no last flight date; that is modelled as
    ``None`` (unknown) rather than ``True``, because "never flown" and "has not
    flown recently" are different states and conflating them would mis-target
    re-engagement campaigns.
    """
    if last_flight_date is None:
        return None
    return (as_of - last_flight_date).days > threshold_days


def stage_member(record: RawRecord, config: PipelineConfig) -> StagedMember:
    """Cast one landed record into a staged, validated member row."""
    member = StagedMember(
        source_file=record.source_file,
        line_number=record.line_number,
        batch_date=record.batch_date,
    )
    member.issues.extend(validate_record(record))

    member.member_name = clean_text(record.get("member_name"))
    member.member_id = clean_text(record.get("member_id"))
    member.tier_code = upper(record.get("tier_code"))
    member.agent_name = clean_text(record.get("agent_name"))
    member.state = upper(record.get("state"))
    member.country = upper(record.get("country"))
    member.active_member = upper(record.get("active_member"))

    for column in ("enrollment_date", "last_flight_date", "date_of_birth"):
        spec = FIELDS_BY_NAME[column]
        value, issue = parse_source_date(
            record.get(column), column, spec.date_format, spec.alternate_date_formats
        )
        setattr(member, column, value)
        if issue:
            member.issues.append(issue)

    post_code, issue = parse_int(record.get("post_code"), "post_code")
    member.post_code = post_code
    if issue:
        member.issues.append(issue)

    member.country_code = conform_country(member.country)
    member.target_table = target_table(member.country_code, config.target_table_prefix)
    member.age = calculate_age(member.date_of_birth, config.as_of_date)
    member.stale_member = is_stale(
        member.last_flight_date, config.as_of_date, config.stale_after_days
    )

    member.issues.extend(validate_member(member, config))
    return member


def stage_members(
    records: Iterable[RawRecord], config: PipelineConfig
) -> Iterator[StagedMember]:
    """Lazily stage a stream of raw records."""
    for record in records:
        yield stage_member(record, config)


def _recency_key(member: StagedMember) -> tuple:
    """Ordering used to decide which duplicate is the "latest" record.

    The feed carries no explicit change timestamp, so recency is derived from
    the strongest available evidence, most trustworthy first:

    1. batch date -- a later delivery supersedes an earlier one;
    2. last flight date -- the most recent real member activity;
    3. enrollment date -- re-enrolment after a move;
    4. line number -- within one file, later physically wins, which keeps the
       resolution deterministic instead of dependent on iteration order.

    Absent dates sort lowest so a fully-populated record beats a sparse one.
    """
    return (
        member.batch_date or date.min,
        member.last_flight_date or date.min,
        member.enrollment_date or date.min,
        member.line_number,
    )


@dataclass
class ResolutionStats:
    seen: int = 0
    superseded: int = 0
    country_moves: int = 0


class LatestRecordResolver:
    """Keeps exactly one row per business key: the latest record wins.

    Country changes are detected as part of resolution: if the winning record
    routes to a different table than a superseded one, the member has moved and
    the stale row must be deleted from the previous country's table.  Returning
    those moves lets the loader clean up rather than leaving the member visible
    in two countries at once.
    """

    def __init__(self) -> None:
        self._winners: dict[str, StagedMember] = {}
        self._previous_tables: dict[str, set[str]] = {}
        self.stats = ResolutionStats()

    def add(self, member: StagedMember) -> None:
        key = member.member_id
        if not key:
            return  # missing business key: already an ERROR, handled as quarantine
        self.stats.seen += 1

        incumbent = self._winners.get(key)
        if incumbent is None:
            self._winners[key] = member
            return

        self.stats.superseded += 1
        loser, winner = (
            (incumbent, member)
            if _recency_key(member) >= _recency_key(incumbent)
            else (member, incumbent)
        )
        if loser.target_table != winner.target_table:
            self.stats.country_moves += 1
            self._previous_tables.setdefault(key, set()).add(loser.target_table)
            winner.issues.append(
                Issue(
                    rule="country_move",
                    severity=Severity.WARNING,
                    message=(
                        f"member moved from {loser.country_code} to "
                        f"{winner.country_code}; the row must be removed from "
                        f"{loser.target_table}"
                    ),
                    column="country",
                )
            )
        self._winners[key] = winner

    def winners(self) -> Iterator[StagedMember]:
        return iter(self._winners.values())

    def stale_locations(self) -> dict[str, set[str]]:
        """Member IDs mapped to the target tables they must be removed from."""
        return {
            member_id: tables - {self._winners[member_id].target_table}
            for member_id, tables in self._previous_tables.items()
            if tables - {self._winners[member_id].target_table}
        }


def route_by_country(members: Iterable[StagedMember]) -> dict[str, list[StagedMember]]:
    """Group resolved members by their per-country target table."""
    routed: dict[str, list[StagedMember]] = {}
    for member in members:
        routed.setdefault(member.target_table, []).append(member)
    return routed
