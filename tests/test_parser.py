"""Parser behaviour: framing, header binding and schema drift."""

from __future__ import annotations

import pytest

from skypoints.parser import FlatFileError, FlatFileReader, parse_filename, split_record

from helpers import HEADER


def test_framing_pipes_do_not_create_phantom_fields():
    # A naive split on '|' yields a leading empty field, which would shift
    # every column by one and silently corrupt the whole file.
    assert split_record("|D|Elena|223457|") == ["D", "Elena", "223457"]


def test_reads_the_sample_file_from_the_brief(clean_feed):
    reader = FlatFileReader(clean_feed)
    records = list(reader.records())

    assert len(records) == 5
    assert records[0].values["member_name"] == "Elena"
    assert records[0].values["country"] == "USA"
    assert records[-1].values["member_id"] == "2256"


def test_missing_optional_column_is_reported_not_silently_shifted(clean_feed):
    # The design document declares Post Code at position 9; the sample file
    # does not send it. Binding by name means the columns after it still land
    # correctly, and the absence is reported rather than hidden.
    reader = FlatFileReader(clean_feed)
    records = list(reader.records())

    assert "post_code" in reader.missing_columns
    assert records[0].values["date_of_birth"] == "03051985"
    assert records[0].values["active_member"] == "A"
    assert any(i.rule == "schema_drift" for i in reader.file_issues)


def test_unknown_header_column_is_flagged(write_feed):
    path = write_feed([HEADER + "|Loyalty_Segment", "|D|Elena|223457|20101012|20121013|GLD|Sam|CA|USA|03051985|A|VIP"])
    reader = FlatFileReader(path)
    list(reader.records())

    assert reader.unknown_columns == ["Loyalty_Segment"]


def test_detail_before_header_is_fatal(write_feed):
    # Continuing here would mean guessing the column order, so the file is
    # rejected outright rather than loaded incorrectly.
    path = write_feed(["|D|Elena|223457|20101012|20121013|GLD|Sam|CA|USA|03051985|A"])
    with pytest.raises(FlatFileError, match="before any"):
        list(FlatFileReader(path).records())


def test_missing_header_is_fatal(write_feed):
    path = write_feed(["", "   "])
    with pytest.raises(FlatFileError, match="no .* header"):
        list(FlatFileReader(path).records())


def test_unknown_record_tag_is_skipped_with_a_warning(write_feed):
    path = write_feed([HEADER, "|X|junk", "|D|Elena|223457|20101012|20121013|GLD|Sam|CA|USA|03051985|A"])
    reader = FlatFileReader(path)
    records = list(reader.records())

    assert len(records) == 1
    assert any(i.rule == "record_tag" for i in reader.file_issues)


def test_short_record_is_marked_rather_than_crashing(write_feed):
    path = write_feed([HEADER, "|D|Elena|223457|20101012"])
    reader = FlatFileReader(path)
    record = next(iter(reader.records()))

    assert "__field_count_mismatch__" in record.values


def test_batch_date_comes_from_the_filename(clean_feed):
    meta = parse_filename(clean_feed)
    assert meta.batch_date.isoformat() == "2024-01-15"
    assert meta.batch_time == "10300000"
    assert meta.issues == []


def test_non_conforming_filename_warns_but_still_processes(write_feed):
    path = write_feed([HEADER], name="members.dat")
    meta = parse_filename(path)

    assert meta.batch_date is not None
    assert any(i.rule == "filename_specification" for i in meta.issues)


def test_parsing_is_lazy(clean_feed):
    # The stream must not materialise: this is what keeps memory flat at the
    # billions-of-rows scale the brief specifies.
    stream = FlatFileReader(clean_feed).records()
    assert next(stream).values["member_name"] == "Elena"
