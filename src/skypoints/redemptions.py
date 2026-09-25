"""Flatten the partner JSON redemption feed into a queryable relation.

The feed arrives as one document per member::

    {"member_id": "223457", "feed_date": "20240115",
     "redemptions": [{"txn_id": "RX10091", ...}, ...]}

which is a nested, awkward shape to query.  It is exploded to one row per
transaction at grain ``txn_id`` and enriched with the parent's ``member_id``
and ``feed_date`` so that every row is self-describing and joinable on its own.

Three input shapes are accepted, because partner feeds at scale rarely stay in
one form:

* a single JSON object,
* a JSON array of such objects,
* JSON Lines (one object per line) -- the preferred form at volume, since it
  is splittable and can be streamed without holding the file in memory.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from .coerce import clean_text, parse_source_date, upper
from .config import PipelineConfig
from .models import Issue, Redemption, Severity
from .parser import parse_filename
from .spec import REDEMPTION_FILE_PATTERN
from .validation import validate_redemption


class RedemptionFeedError(Exception):
    """Raised when a feed file contains no parseable JSON at all."""


def iter_documents(path: Path, encoding: str = "utf-8") -> Iterator[dict[str, Any]]:
    """Yield member documents from a JSON / JSON Lines feed.

    JSON Lines is streamed line by line so a multi-terabyte feed never lands in
    memory.  A whole-file object or array is parsed as a fallback for the
    smaller deliveries the brief illustrates.
    """
    with path.open("r", encoding=encoding) as handle:
        first_chunk = handle.read(1)
        while first_chunk and first_chunk.isspace():
            first_chunk = handle.read(1)
        handle.seek(0)

        if first_chunk == "{":
            # Could be a single object or JSON Lines; try line-wise first.
            lines = [line for line in handle if line.strip()]
            documents: list[dict[str, Any]] = []
            try:
                for line in lines:
                    documents.append(json.loads(line))
            except json.JSONDecodeError:
                documents = []
            if documents:
                yield from documents
                return
            payload = json.loads("".join(lines))
            yield from (payload if isinstance(payload, list) else [payload])
            return

        if first_chunk == "[":
            payload = json.load(handle)
            yield from payload
            return

    raise RedemptionFeedError(f"{path.name}: not a JSON object, array or JSON Lines feed")


def flatten_document(
    document: dict[str, Any],
    source_file: str,
    batch_date,
    config: PipelineConfig,
    start_index: int = 0,
) -> Iterator[Redemption]:
    """Explode one member document into one row per transaction."""
    member_id = clean_text(str(document.get("member_id"))) if document.get("member_id") else None
    feed_date, feed_issue = parse_source_date(
        str(document["feed_date"]) if document.get("feed_date") else None, "feed_date"
    )

    transactions = document.get("redemptions") or []
    if not isinstance(transactions, list):
        transactions = [transactions]

    if not transactions:
        # A member document with no transactions is legitimate (nothing was
        # redeemed today) but is surfaced so a partner silently sending empty
        # payloads is noticed rather than looking like healthy throughput.
        redemption = Redemption(
            member_id=member_id,
            feed_date=feed_date,
            source_file=source_file,
            record_index=start_index,
            batch_date=batch_date,
        )
        redemption.issues.append(
            Issue(
                rule="empty_redemption_array",
                severity=Severity.WARNING,
                message=f"member {member_id!r} sent a document with no redemptions",
            )
        )
        redemption.issues.append(
            Issue(
                rule="mandatory_field",
                severity=Severity.ERROR,
                message="no transaction to load",
                column="txn_id",
            )
        )
        yield redemption
        return

    for offset, txn in enumerate(transactions):
        record = Redemption(
            member_id=member_id,
            feed_date=feed_date,
            source_file=source_file,
            record_index=start_index + offset,
            batch_date=batch_date,
        )
        if feed_issue:
            record.issues.append(feed_issue)

        if not isinstance(txn, dict):
            record.issues.append(
                Issue(
                    rule="malformed_transaction",
                    severity=Severity.ERROR,
                    message=f"expected a JSON object, got {type(txn).__name__}",
                )
            )
            yield record
            continue

        record.txn_id = clean_text(str(txn["txn_id"])) if txn.get("txn_id") else None
        record.partner = clean_text(txn.get("partner"))
        record.status = upper(txn.get("status"))

        txn_date, issue = parse_source_date(
            str(txn["txn_date"]) if txn.get("txn_date") else None, "txn_date"
        )
        record.txn_date = txn_date
        if issue:
            record.issues.append(issue)

        miles = txn.get("miles_redeemed")
        if miles is None:
            record.miles_redeemed = None
        elif isinstance(miles, bool):
            record.issues.append(
                Issue(
                    rule="integer_format",
                    severity=Severity.ERROR,
                    message="miles_redeemed is a boolean",
                    column="miles_redeemed",
                )
            )
        else:
            try:
                record.miles_redeemed = int(miles)
            except (TypeError, ValueError):
                record.issues.append(
                    Issue(
                        rule="integer_format",
                        severity=Severity.ERROR,
                        message=f"{miles!r} is not an integer",
                        column="miles_redeemed",
                    )
                )

        record.issues.extend(validate_redemption(record, config))
        yield record


def read_redemptions(
    path: Path | str, config: PipelineConfig
) -> Iterator[Redemption]:
    """Stream a redemption feed file as flattened transaction rows."""
    path = Path(path)
    meta = parse_filename(path, REDEMPTION_FILE_PATTERN)
    batch_date = meta.batch_date or config.as_of_date

    index = 0
    for document in iter_documents(path):
        for record in flatten_document(document, path.name, batch_date, config, index):
            index += 1
            yield record


def deduplicate(records: Iterable[Redemption]) -> Iterator[Redemption]:
    """Keep one row per ``txn_id``, preferring the most recently stated status.

    Partners re-send transactions as their status advances (PENDING ->
    COMPLETED), so the transaction table is a current-state view at txn grain
    rather than an append-only log of every restatement.
    """
    status_rank = {"PENDING": 0, "FAILED": 1, "CANCELLED": 2, "REVERSED": 3, "COMPLETED": 4}
    best: dict[str, Redemption] = {}
    order: list[str] = []

    for record in records:
        if not record.txn_id:
            continue
        incumbent = best.get(record.txn_id)
        if incumbent is None:
            best[record.txn_id] = record
            order.append(record.txn_id)
            continue
        candidate_key = (record.feed_date or record.batch_date, status_rank.get(record.status or "", -1))
        incumbent_key = (
            incumbent.feed_date or incumbent.batch_date,
            status_rank.get(incumbent.status or "", -1),
        )
        if candidate_key >= incumbent_key:
            best[record.txn_id] = record

    for txn_id in order:
        yield best[txn_id]
