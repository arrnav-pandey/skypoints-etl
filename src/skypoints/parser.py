"""Streaming parser for the pipe-delimited member profile flat file.

Design notes
------------
* **Streaming.**  The brief specifies billions of records per day, so the
  reader never materialises the file.  It yields one :class:`RawRecord` at a
  time and holds only header metadata in memory, which keeps memory flat
  regardless of file size and lets the same code run over a 10-row sample or a
  multi-terabyte feed.
* **Header-driven binding.**  Columns are bound by the names in the ``|H|``
  record rather than by ordinal position, so an absent optional column (the
  sample file has no ``Post Code``) shifts nothing.
* **Parse, don't reject.**  Structural problems are attached to the record as
  issues; the reader itself only raises when the file is unusable (no header).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .models import Issue, RawRecord, Severity
from .spec import (
    DETAIL_TAG,
    HEADER_TAG,
    MEMBER_FILE_PATTERN,
    MEMBER_LAYOUT,
    RECORD_SEPARATOR,
    SOURCE_DATE_FORMAT,
    FieldSpec,
    resolve_header,
)


class FlatFileError(Exception):
    """Raised when a file cannot be parsed at all (e.g. missing header)."""


@dataclass
class FileMetadata:
    """What the filename specification tells us about a batch."""

    path: Path
    batch_date: date | None = None
    batch_time: str | None = None
    issues: list[Issue] = field(default_factory=list)


def parse_filename(path: Path, pattern: str = MEMBER_FILE_PATTERN) -> FileMetadata:
    """Derive the batch date/time from the filename specification.

    Expected form ``<PREFIX>_YYYYMMDD_HHMMSSTT.<ext>``.  A non-conforming name
    is a warning, not a failure: the batch date then falls back to the file's
    modification date so a mis-named delivery can still be processed and
    flagged rather than blocking the whole run.
    """
    meta = FileMetadata(path=path)
    match = re.match(pattern, path.name, flags=re.IGNORECASE)
    if not match:
        meta.issues.append(
            Issue(
                rule="filename_specification",
                severity=Severity.WARNING,
                message=(
                    f"{path.name!r} does not match the filename specification "
                    f"{pattern!r}; falling back to file modification date"
                ),
            )
        )
        meta.batch_date = date.fromtimestamp(path.stat().st_mtime)
        return meta

    try:
        meta.batch_date = datetime.strptime(match.group("date"), SOURCE_DATE_FORMAT).date()
    except ValueError:
        meta.issues.append(
            Issue(
                rule="filename_specification",
                severity=Severity.WARNING,
                message=f"filename carries an invalid date {match.group('date')!r}",
            )
        )
        meta.batch_date = date.fromtimestamp(path.stat().st_mtime)
    meta.batch_time = match.group("time")
    return meta


def split_record(line: str) -> list[str]:
    """Split a ``|``-delimited record into its fields.

    Records are framed by a leading (and optionally trailing) separator, e.g.
    ``|D|Elena|223457|...``.  The framing separators are stripped so that an
    empty first field is not mistaken for a missing column.
    """
    stripped = line.strip("\r\n")
    if stripped.startswith(RECORD_SEPARATOR):
        stripped = stripped[1:]
    if stripped.endswith(RECORD_SEPARATOR):
        stripped = stripped[:-1]
    return stripped.split(RECORD_SEPARATOR)


@dataclass
class FlatFileReader:
    """Reads one member flat file, exposing header metadata and detail rows."""

    path: Path
    encoding: str = "utf-8"
    bound_fields: list[FieldSpec | None] = field(default_factory=list)
    unknown_columns: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    metadata: FileMetadata | None = None
    file_issues: list[Issue] = field(default_factory=list)
    detail_count: int = 0

    def records(self) -> Iterator[RawRecord]:
        """Yield every detail record in the file, header bound lazily."""
        self.metadata = parse_filename(self.path)
        self.file_issues.extend(self.metadata.issues)
        batch_date = self.metadata.batch_date or date.today()

        with self.path.open("r", encoding=self.encoding, newline="") as handle:
            header_seen = False
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                fields = split_record(line)
                tag = fields[0].strip().upper() if fields else ""

                if tag == HEADER_TAG:
                    if header_seen:
                        self.file_issues.append(
                            Issue(
                                rule="single_header",
                                severity=Severity.WARNING,
                                message=f"additional header record at line {line_number} ignored",
                            )
                        )
                        continue
                    self._bind_header(fields[1:])
                    header_seen = True
                    continue

                if not header_seen:
                    raise FlatFileError(
                        f"{self.path.name}: detail record at line {line_number} "
                        "appears before any |H| header record"
                    )

                if tag != DETAIL_TAG:
                    self.file_issues.append(
                        Issue(
                            rule="record_tag",
                            severity=Severity.WARNING,
                            message=f"line {line_number}: unknown record tag {tag!r}, skipped",
                        )
                    )
                    continue

                self.detail_count += 1
                yield self._to_record(fields, line_number, line, batch_date)

            if not header_seen:
                raise FlatFileError(f"{self.path.name}: no |H| header record found")

    def _bind_header(self, header_names: list[str]) -> None:
        self.bound_fields, self.unknown_columns = resolve_header(header_names)
        bound_names = {f.name for f in self.bound_fields if f is not None}
        self.missing_columns = [f.name for f in MEMBER_LAYOUT if f.name not in bound_names]

        if self.unknown_columns:
            self.file_issues.append(
                Issue(
                    rule="schema_drift",
                    severity=Severity.WARNING,
                    message=(
                        "header carries columns absent from the design document: "
                        + ", ".join(self.unknown_columns)
                    ),
                )
            )
        for name in self.missing_columns:
            spec = next(f for f in MEMBER_LAYOUT if f.name == name)
            self.file_issues.append(
                Issue(
                    rule="schema_drift",
                    severity=Severity.ERROR if spec.mandatory else Severity.WARNING,
                    message=(
                        f"column {name!r} declared at file position {spec.position} "
                        "in the design document is absent from the file header"
                    ),
                    column=name,
                )
            )

    def _to_record(
        self, fields: list[str], line_number: int, raw_line: str, batch_date: date
    ) -> RawRecord:
        payload = fields[1:]
        values: dict[str, str | None] = {f.name: None for f in MEMBER_LAYOUT}

        for index, spec in enumerate(self.bound_fields):
            if spec is None:
                continue
            raw = payload[index] if index < len(payload) else None
            values[spec.name] = raw.strip() if raw is not None and raw.strip() else None

        record = RawRecord(
            source_file=self.path.name,
            line_number=line_number,
            record_tag=DETAIL_TAG,
            values=values,
            raw_line=raw_line.rstrip("\r\n"),
            batch_date=batch_date,
        )
        if len(payload) != len(self.bound_fields):
            record.values["__field_count_mismatch__"] = (
                f"expected {len(self.bound_fields)}, got {len(payload)}"
            )
        return record


def read_members(path: Path | str) -> tuple[FlatFileReader, Iterator[RawRecord]]:
    """Convenience entry point returning the reader and its record stream."""
    reader = FlatFileReader(Path(path))
    return reader, reader.records()
