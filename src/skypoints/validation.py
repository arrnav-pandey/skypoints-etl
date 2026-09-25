"""Data validation rules.

Rules are split into three tiers so each can run where it is cheapest:

``record``  cheap, single-row, pre-cast checks (mandatory, length, tag)
``row``     single-row checks that need typed values (domains, cross-field)
``batch``   checks that need to see more than one row (key uniqueness)

Severity is a first-class decision.  ``ERROR`` quarantines the record;
``WARNING`` lets it through but surfaces in the run report.  Nothing is ever
dropped silently -- quarantined rows are written out with their reasons so the
source system can be given actionable feedback.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from .config import (
    UNKNOWN_COUNTRY,
    VALID_ACTIVE_FLAGS,
    VALID_REDEMPTION_STATUSES,
    VALID_TIER_CODES,
    PipelineConfig,
)
from .models import Issue, RawRecord, Redemption, Severity, StagedMember
from .spec import MANDATORY_FIELDS, MEMBER_LAYOUT

FIELD_COUNT_SENTINEL = "__field_count_mismatch__"


def validate_record(record: RawRecord) -> list[Issue]:
    """Structural and contract checks that need no type casting."""
    issues: list[Issue] = []

    mismatch = record.values.get(FIELD_COUNT_SENTINEL)
    if mismatch:
        issues.append(
            Issue(
                rule="field_count",
                severity=Severity.ERROR,
                message=f"field count does not match the header record ({mismatch})",
            )
        )

    for name in MANDATORY_FIELDS:
        if not record.get(name):
            issues.append(
                Issue(
                    rule="mandatory_field",
                    severity=Severity.ERROR,
                    message=f"mandatory column {name!r} is empty",
                    column=name,
                )
            )

    for spec in MEMBER_LAYOUT:
        value = record.get(spec.name)
        if value is not None and len(value) > spec.length:
            issues.append(
                Issue(
                    rule="max_length",
                    severity=Severity.ERROR,
                    message=(
                        f"length {len(value)} exceeds the declared "
                        f"{spec.data_type.value}({spec.length})"
                    ),
                    column=spec.name,
                )
            )

    return issues


def validate_member(member: StagedMember, config: PipelineConfig) -> list[Issue]:
    """Domain and cross-field checks over a cast, conformed member."""
    issues: list[Issue] = []

    if member.tier_code and member.tier_code not in VALID_TIER_CODES:
        issues.append(
            Issue(
                rule="tier_code_domain",
                severity=Severity.WARNING,
                message=(
                    f"{member.tier_code!r} is outside the known tier set "
                    f"{sorted(VALID_TIER_CODES)}"
                ),
                column="tier_code",
            )
        )

    if member.active_member and member.active_member not in VALID_ACTIVE_FLAGS:
        issues.append(
            Issue(
                rule="active_flag_domain",
                severity=Severity.ERROR,
                message=(
                    f"{member.active_member!r} is not one of "
                    f"{sorted(VALID_ACTIVE_FLAGS)}"
                ),
                column="active_member",
            )
        )

    # Country drives the target table, so an unmappable value is a routing
    # failure, not a cosmetic one: the record would land in no country table.
    if member.country_code == UNKNOWN_COUNTRY:
        issues.append(
            Issue(
                rule="country_conformance",
                severity=Severity.ERROR,
                message=(
                    f"country {member.country!r} could not be conformed to an "
                    "ISO alpha-3 code, so the record cannot be routed"
                ),
                column="country",
            )
        )

    # The sample data shows Agent_Name populated for one member and blank for
    # the rest. It is optional per the contract, so this is a completeness
    # signal for the source system rather than a rejection.
    if not member.agent_name:
        issues.append(
            Issue(
                rule="optional_completeness",
                severity=Severity.WARNING,
                message="agent_name is absent",
                column="agent_name",
            )
        )

    if member.enrollment_date and member.last_flight_date:
        if member.last_flight_date < member.enrollment_date:
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

    if member.date_of_birth:
        if member.date_of_birth.year < config.min_birth_year:
            issues.append(
                Issue(
                    rule="dob_plausible",
                    severity=Severity.ERROR,
                    message=(
                        f"date_of_birth {member.date_of_birth} is before "
                        f"{config.min_birth_year}"
                    ),
                    column="date_of_birth",
                )
            )
        if member.date_of_birth > config.as_of_date:
            issues.append(
                Issue(
                    rule="dob_not_future",
                    severity=Severity.ERROR,
                    message=f"date_of_birth {member.date_of_birth} is in the future",
                    column="date_of_birth",
                )
            )
        if member.enrollment_date and member.date_of_birth > member.enrollment_date:
            issues.append(
                Issue(
                    rule="dob_before_enrollment",
                    severity=Severity.ERROR,
                    message="date_of_birth is after enrollment_date",
                    column="date_of_birth",
                )
            )

    for column, value in (
        ("enrollment_date", member.enrollment_date),
        ("last_flight_date", member.last_flight_date),
    ):
        if value and value > config.as_of_date:
            issues.append(
                Issue(
                    rule="date_not_future",
                    severity=Severity.ERROR,
                    message=f"{column} {value} is after the batch date {config.as_of_date}",
                    column=column,
                )
            )

    return issues


def validate_redemption(
    redemption: Redemption, config: PipelineConfig
) -> list[Issue]:
    issues: list[Issue] = []

    if not redemption.txn_id:
        issues.append(
            Issue(
                rule="mandatory_field",
                severity=Severity.ERROR,
                message="txn_id is mandatory -- it is the transaction grain",
                column="txn_id",
            )
        )
    if not redemption.member_id:
        issues.append(
            Issue(
                rule="mandatory_field",
                severity=Severity.ERROR,
                message="member_id is mandatory -- it is the join key to the profile",
                column="member_id",
            )
        )
    if redemption.miles_redeemed is None:
        issues.append(
            Issue(
                rule="mandatory_field",
                severity=Severity.ERROR,
                message="miles_redeemed is mandatory",
                column="miles_redeemed",
            )
        )
    elif redemption.miles_redeemed < 0:
        issues.append(
            Issue(
                rule="miles_non_negative",
                severity=Severity.ERROR,
                message=f"miles_redeemed {redemption.miles_redeemed} is negative",
                column="miles_redeemed",
            )
        )

    if redemption.status and redemption.status not in VALID_REDEMPTION_STATUSES:
        issues.append(
            Issue(
                rule="status_domain",
                severity=Severity.WARNING,
                message=(
                    f"{redemption.status!r} is outside the known status set "
                    f"{sorted(VALID_REDEMPTION_STATUSES)}"
                ),
                column="status",
            )
        )

    if redemption.txn_date and redemption.txn_date > config.as_of_date:
        issues.append(
            Issue(
                rule="date_not_future",
                severity=Severity.ERROR,
                message=f"txn_date {redemption.txn_date} is after the batch date",
                column="txn_date",
            )
        )

    if redemption.txn_date and redemption.feed_date and redemption.txn_date > redemption.feed_date:
        issues.append(
            Issue(
                rule="txn_within_feed_window",
                severity=Severity.WARNING,
                message=(
                    f"txn_date {redemption.txn_date} is later than the feed_date "
                    f"{redemption.feed_date}"
                ),
                column="txn_date",
            )
        )

    return issues


class UniquenessTracker:
    """Detects duplicate key values within a batch.

    Holds only a hash of each key, not the row, so the footprint is bounded by
    distinct key count rather than by data volume.  At the stated scale the
    authoritative uniqueness check still belongs in the warehouse (see
    ``sql/07_validations.sql``); this tracker gives the pipeline a fast,
    in-flight signal so a bad delivery fails before it is loaded.
    """

    def __init__(self, key_columns: Iterable[str]) -> None:
        self.key_columns = tuple(key_columns)
        self._seen: set[int] = set()
        self.duplicate_counts: Counter[str] = Counter()

    def _key_of(self, member: StagedMember) -> str:
        return "\x1f".join(str(getattr(member, column, None)) for column in self.key_columns)

    def check(self, member: StagedMember) -> Issue | None:
        key = self._key_of(member)
        digest = hash(key)
        if digest in self._seen:
            self.duplicate_counts[key] += 1
            return Issue(
                rule="key_uniqueness",
                severity=Severity.WARNING,
                message=(
                    f"duplicate value for key {'+'.join(self.key_columns)}={key!r}; "
                    "resolved by the latest-record-wins rule"
                ),
                column=self.key_columns[0],
            )
        self._seen.add(digest)
        return None

    @property
    def distinct_keys(self) -> int:
        return len(self._seen)


def declared_key_note() -> str:
    """Explain the divergence between the declared and effective key."""
    declared = [f.name for f in MEMBER_LAYOUT if f.key_column]
    business = [f.name for f in MEMBER_LAYOUT if f.business_key]
    return (
        f"design document declares {declared} as the key column(s); the pipeline "
        f"deduplicates on {business} because member names are not unique"
    )


def summarise(issues: Iterable[Issue]) -> Counter[str]:
    """Roll issues up by ``severity:rule`` for the run report."""
    return Counter(f"{issue.severity.value}:{issue.rule}" for issue in issues)


__all__ = [
    "FIELD_COUNT_SENTINEL",
    "UniquenessTracker",
    "declared_key_note",
    "summarise",
    "validate_member",
    "validate_record",
    "validate_redemption",
]
