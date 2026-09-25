"""Runtime configuration: country conformance and target-table routing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

#: Source country codes are inconsistent in the sample data -- ISO alpha-3 for
#: some rows (USA, IND, CAN), truncated free text for others (PHIL, AU).  The
#: pipeline conforms everything to ISO 3166-1 alpha-3 so that routing is stable
#: and a member who "moves" from ``AU`` to ``AUS`` is not treated as a move.
COUNTRY_ALIASES: dict[str, str] = {
    "USA": "USA", "US": "USA", "UNITED STATES": "USA",
    "IND": "IND", "IN": "IND", "INDIA": "IND",
    "PHIL": "PHL", "PHL": "PHL", "PH": "PHL", "PHILIPPINES": "PHL",
    "CAN": "CAN", "CA": "CAN", "CANADA": "CAN",
    "AU": "AUS", "AUS": "AUS", "AUSTRALIA": "AUS",
    "GBR": "GBR", "UK": "GBR", "GB": "GBR",
    "ARE": "ARE", "UAE": "ARE",
    "SGP": "SGP", "SG": "SGP",
    "DEU": "DEU", "DE": "DEU",
    "JPN": "JPN", "JP": "JPN",
}

#: Members whose country cannot be conformed are quarantined here rather than
#: dropped, so no record is ever lost silently.
UNKNOWN_COUNTRY = "UNK"

#: Human-readable names used to build target table identifiers.
COUNTRY_TABLE_NAMES: dict[str, str] = {
    "USA": "USA",
    "IND": "INDIA",
    "PHL": "PHILIPPINES",
    "CAN": "CANADA",
    "AUS": "AUSTRALIA",
    "GBR": "UNITED_KINGDOM",
    "ARE": "UAE",
    "SGP": "SINGAPORE",
    "DEU": "GERMANY",
    "JPN": "JAPAN",
    UNKNOWN_COUNTRY: "UNKNOWN",
}

VALID_TIER_CODES: frozenset[str] = frozenset({"BAS", "SLV", "GLD", "PLT", "DIA"})
VALID_ACTIVE_FLAGS: frozenset[str] = frozenset({"A", "I"})
VALID_REDEMPTION_STATUSES: frozenset[str] = frozenset(
    {"COMPLETED", "PENDING", "CANCELLED", "REVERSED", "FAILED"}
)


def conform_country(raw: str | None) -> str:
    """Map a raw source country value onto its ISO alpha-3 code."""
    if not raw:
        return UNKNOWN_COUNTRY
    return COUNTRY_ALIASES.get(raw.strip().upper(), UNKNOWN_COUNTRY)


def target_table(country_code: str, prefix: str = "TABLE") -> str:
    """Return the per-country target table name, e.g. ``TABLE_INDIA``."""
    suffix = COUNTRY_TABLE_NAMES.get(country_code, COUNTRY_TABLE_NAMES[UNKNOWN_COUNTRY])
    return f"{prefix}_{suffix}"


@dataclass(frozen=True)
class PipelineConfig:
    """Tunables for a single pipeline run."""

    #: Date the batch is processed for.  Injected rather than read from the
    #: clock so that Age and Stale_Member are deterministic and re-runnable.
    as_of_date: date

    #: Days since the last flight beyond which a member is considered stale.
    stale_after_days: int = 90

    #: Oldest plausible date of birth, used as a sanity check.
    min_birth_year: int = 1900

    #: Rows failing a blocking rule are written here instead of the target.
    quarantine_enabled: bool = True

    target_table_prefix: str = "TABLE"

    #: Countries expected in a normal batch; anything else raises a warning so
    #: an unexpected new market is noticed rather than silently quarantined.
    expected_countries: frozenset[str] = field(
        default_factory=lambda: frozenset(COUNTRY_TABLE_NAMES) - {UNKNOWN_COUNTRY}
    )
