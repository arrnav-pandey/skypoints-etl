"""Batch orchestration: land -> stage -> resolve -> route -> load, with a report.

The local sink writes CSV so the pipeline is demonstrable end to end with no
warehouse attached.  The stage boundaries are deliberately the same ones the
Snowflake implementation uses (see ``sql/``), so swapping the writer for a
``COPY INTO`` / ``MERGE`` does not disturb the logic above it.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import PipelineConfig
from .models import Redemption, Severity, StagedMember
from .parser import FlatFileReader
from .redemptions import deduplicate, read_redemptions
from .sources import CrossCountryIdTracker, iter_source
from .transform import LatestRecordResolver, route_by_country, stage_members
from .validation import UniquenessTracker, declared_key_note, summarise

MEMBER_COLUMNS = list(StagedMember().to_row().keys())
REDEMPTION_COLUMNS = list(Redemption().to_row().keys())


@dataclass
class RunReport:
    """Everything an operator needs to decide whether a batch is trustworthy."""

    batch_date: str
    members_read: int = 0
    members_quarantined: int = 0
    members_loaded: int = 0
    members_superseded: int = 0
    country_moves: int = 0
    distinct_member_keys: int = 0
    redemptions_read: int = 0
    redemptions_quarantined: int = 0
    redemptions_loaded: int = 0
    rows_per_target: dict[str, int] = field(default_factory=dict)
    stale_locations: dict[str, list[str]] = field(default_factory=dict)
    id_collisions: dict[str, list[str]] = field(default_factory=dict)
    issue_counts: dict[str, int] = field(default_factory=dict)
    file_issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _quarantine_rows(rows: list[tuple[dict[str, Any], list[str]]]) -> list[dict[str, Any]]:
    return [{**row, "rejection_reasons": " | ".join(reasons)} for row, reasons in rows]


def run(
    member_file: Path | str,
    redemption_file: Path | str | None,
    output_dir: Path | str,
    config: PipelineConfig,
) -> RunReport:
    """Execute one daily batch and return its report."""
    output_dir = Path(output_dir)
    report = RunReport(batch_date=config.as_of_date.isoformat())
    report.notes.append(declared_key_note())
    issue_counts: Counter[str] = Counter()

    # --- members -----------------------------------------------------------
    reader = FlatFileReader(Path(member_file))
    resolver = LatestRecordResolver()
    uniqueness = UniquenessTracker(["member_id"])
    quarantined: list[tuple[dict[str, Any], list[str]]] = []

    for member in stage_members(reader.records(), config):
        report.members_read += 1
        duplicate = uniqueness.check(member)
        if duplicate:
            member.issues.append(duplicate)
        issue_counts.update(summarise(member.issues))

        if not member.is_valid:
            report.members_quarantined += 1
            quarantined.append((member.to_row(), [str(i) for i in member.errors]))
            continue
        resolver.add(member)

    report.file_issues = [str(i) for i in reader.file_issues]
    issue_counts.update(summarise(reader.file_issues))

    winners = list(resolver.winners())
    # Country moves are only known once resolution has seen every duplicate,
    # so they are counted here. Other issues were already counted above; only
    # the move warnings are added, to avoid double counting.
    for member in winners:
        issue_counts.update(
            summarise(i for i in member.warnings if i.rule == "country_move")
        )

    routed = route_by_country(winners)
    for table_name, rows in sorted(routed.items()):
        _write_csv(
            output_dir / "targets" / f"{table_name.lower()}.csv",
            MEMBER_COLUMNS,
            [m.to_row() for m in rows],
        )
        report.rows_per_target[table_name] = len(rows)

    report.members_loaded = len(winners)
    report.members_superseded = resolver.stats.superseded
    report.country_moves = resolver.stats.country_moves
    report.distinct_member_keys = uniqueness.distinct_keys
    report.stale_locations = {k: sorted(v) for k, v in resolver.stale_locations().items()}

    # --- redemptions -------------------------------------------------------
    if redemption_file:
        valid: list[Redemption] = []
        for record in read_redemptions(redemption_file, config):
            report.redemptions_read += 1
            issue_counts.update(summarise(record.issues))
            if record.is_valid:
                valid.append(record)
            else:
                report.redemptions_quarantined += 1
                quarantined.append(
                    (
                        record.to_row(),
                        [str(i) for i in record.issues if i.severity.value == "ERROR"],
                    )
                )
        deduped = list(deduplicate(valid))
        report.redemptions_loaded = len(deduped)
        _write_csv(
            output_dir / "targets" / "redemption_txn.csv",
            REDEMPTION_COLUMNS,
            [r.to_row() for r in deduped],
        )

    # --- quarantine + report ----------------------------------------------
    if quarantined and config.quarantine_enabled:
        columns = sorted({key for row, _ in quarantined for key in row} | {"rejection_reasons"})
        _write_csv(output_dir / "quarantine.csv", columns, _quarantine_rows(quarantined))

    report.issue_counts = dict(sorted(issue_counts.items()))
    (output_dir / "run_report.json").write_text(report.to_json(), encoding="utf-8")
    return report


def run_sources(
    member_files: Iterable[Path | str],
    redemption_file: Path | str | None,
    output_dir: Path | str,
    config: PipelineConfig,
) -> RunReport:
    """Run a batch over the per-country member feeds.

    The country feeds differ from the pipe-delimited flat file in one way that
    changes the shape of this function: each file *is* a country, so routing is
    known before a row is read. Deduplication therefore happens within a
    country rather than across the whole batch, using the composite
    ``(country, member_id)`` key.

    Cross-country ID collisions are reported rather than resolved. See
    :mod:`skypoints.sources` for why merging them would be unrecoverable.
    """
    output_dir = Path(output_dir)
    report = RunReport(batch_date=config.as_of_date.isoformat())
    issue_counts: Counter[str] = Counter()
    quarantined: list[tuple[dict[str, Any], list[str]]] = []

    everyone_tracker = CrossCountryIdTracker()
    by_country: dict[str, LatestRecordResolver] = {}

    for path in member_files:
        path = Path(path)
        try:
            members = iter_source(path, config)
        except LookupError as exc:
            # An unroutable file is a batch-level problem, not a row-level one.
            report.file_issues.append(f"ERROR unroutable_file: {exc}")
            continue

        for member in members:
            report.members_read += 1
            everyone_tracker.observe(member)
            issue_counts.update(summarise(member.issues))

            if not member.is_valid:
                report.members_quarantined += 1
                quarantined.append((member.to_row(), [str(i) for i in member.errors]))
                continue

            resolver = by_country.setdefault(member.country_code, LatestRecordResolver())
            resolver.add(member)

    report.id_collisions = everyone_tracker.collisions()
    if report.id_collisions:
        ambiguous = [
            member_id
            for member_id in report.id_collisions
            if everyone_tracker.names_differ(member_id)
        ]
        report.notes.append(
            f"{len(report.id_collisions)} member_id value(s) appear in more than one "
            f"country ({len(ambiguous)} of them under more than one name). They are "
            "kept separate under the (country, member_id) key rather than merged, "
            "because the data cannot distinguish a relocation from countries "
            "numbering their members independently."
        )

    winners = [m for resolver in by_country.values() for m in resolver.winners()]
    routed = route_by_country(winners)
    for table_name, rows in sorted(routed.items()):
        _write_csv(
            output_dir / "targets" / f"{table_name.lower()}.csv",
            MEMBER_COLUMNS,
            [m.to_row() for m in rows],
        )
        report.rows_per_target[table_name] = len(rows)

    report.members_loaded = len(winners)
    report.members_superseded = sum(r.stats.superseded for r in by_country.values())
    report.distinct_member_keys = everyone_tracker.distinct_keys

    if redemption_file:
        _load_redemptions(redemption_file, config, output_dir, report, issue_counts, quarantined)

    if quarantined and config.quarantine_enabled:
        columns = sorted({key for row, _ in quarantined for key in row} | {"rejection_reasons"})
        _write_csv(output_dir / "quarantine.csv", columns, _quarantine_rows(quarantined))

    report.issue_counts = dict(sorted(issue_counts.items()))
    (output_dir / "run_report.json").write_text(report.to_json(), encoding="utf-8")
    return report


def _load_redemptions(
    redemption_file: Path | str,
    config: PipelineConfig,
    output_dir: Path,
    report: RunReport,
    issue_counts: Counter[str],
    quarantined: list[tuple[dict[str, Any], list[str]]],
) -> None:
    valid: list[Redemption] = []
    for record in read_redemptions(redemption_file, config):
        report.redemptions_read += 1
        issue_counts.update(summarise(record.issues))
        if record.is_valid:
            valid.append(record)
        else:
            report.redemptions_quarantined += 1
            quarantined.append(
                (record.to_row(), [str(i) for i in record.issues if i.severity is Severity.ERROR])
            )

    deduped = list(deduplicate(valid))
    report.redemptions_loaded = len(deduped)
    _write_csv(
        output_dir / "targets" / "redemption_txn.csv",
        REDEMPTION_COLUMNS,
        [r.to_row() for r in deduped],
    )
