"""Tests for vncvt.cast_recorder: header format, output/input/resize
event recording, close semantics, and UTF-8 incremental decoding."""

from __future__ import annotations

import json

import pytest

from vncvt.cast_recorder import CastRecorder


# -- helpers ---------------------------------------------------------

def _read_lines(path):
    """Return all non-empty lines from a cast file."""
    return [l for l in path.read_text("utf-8").splitlines() if l]


def _make_recorder(tmp_path, *, cols=80, rows=24, record_input=False):
    return CastRecorder(
        path=tmp_path / "test.cast",
        cols=cols,
        rows=rows,
        shell="/bin/bash",
        record_input=record_input,
    )


# -- 1. Header line -------------------------------------------------

def test_header_is_valid_json(tmp_path):
    rec = _make_recorder(tmp_path)
    rec.close()

    lines = _read_lines(tmp_path / "test.cast")
    assert len(lines) >= 1
    header = json.loads(lines[0])
    assert isinstance(header, dict)


def test_header_contains_required_keys(tmp_path):
    rec = _make_recorder(tmp_path, cols=120, rows=40)
    rec.close()

    header = json.loads(_read_lines(tmp_path / "test.cast")[0])
    assert header["version"] == 2
    assert header["width"] == 120
    assert header["height"] == 40
    assert "timestamp" in header
    assert isinstance(header["timestamp"], int)


# -- 2. record_output -----------------------------------------------

def test_record_output_appends_o_event(tmp_path):
    rec = _make_recorder(tmp_path)
    rec.record_output(b"hello")
    rec.close()

    lines = _read_lines(tmp_path / "test.cast")
    assert len(lines) == 2  # header + one event
    event = json.loads(lines[1])
    assert len(event) == 3
    ts, kind, text = event
    assert isinstance(ts, float)
    assert ts >= 0
    assert kind == "o"
    assert text == "hello"


def test_record_output_ignores_empty_data(tmp_path):
    rec = _make_recorder(tmp_path)
    rec.record_output(b"")
    rec.close()

    lines = _read_lines(tmp_path / "test.cast")
    assert len(lines) == 1  # header only


def test_record_output_multiple_events(tmp_path):
    rec = _make_recorder(tmp_path)
    rec.record_output(b"first")
    rec.record_output(b"second")
    rec.close()

    lines = _read_lines(tmp_path / "test.cast")
    assert len(lines) == 3
    assert json.loads(lines[1])[2] == "first"
    assert json.loads(lines[2])[2] == "second"
    # timestamps are monotonically non-decreasing
    assert json.loads(lines[2])[0] >= json.loads(lines[1])[0]


# -- 3. record_input ------------------------------------------------

def test_record_input_when_enabled(tmp_path):
    rec = _make_recorder(tmp_path, record_input=True)
    rec.record_input(b"keypress")
    rec.close()

    lines = _read_lines(tmp_path / "test.cast")
    assert len(lines) == 2
    event = json.loads(lines[1])
    ts, kind, text = event
    assert isinstance(ts, float)
    assert kind == "i"
    assert text == "keypress"


def test_record_input_disabled_by_default(tmp_path):
    rec = _make_recorder(tmp_path)
    rec.record_input(b"keypress")
    rec.close()

    lines = _read_lines(tmp_path / "test.cast")
    assert len(lines) == 1  # header only, no input event


def test_record_input_ignores_empty_data(tmp_path):
    rec = _make_recorder(tmp_path, record_input=True)
    rec.record_input(b"")
    rec.close()

    lines = _read_lines(tmp_path / "test.cast")
    assert len(lines) == 1  # header only


# -- 4. resize -------------------------------------------------------

def test_resize_appends_r_event(tmp_path):
    rec = _make_recorder(tmp_path, cols=80, rows=24)
    rec.resize(132, 50)
    rec.close()

    lines = _read_lines(tmp_path / "test.cast")
    assert len(lines) == 2
    event = json.loads(lines[1])
    ts, kind, payload = event
    assert isinstance(ts, float)
    assert kind == "r"
    assert payload == "132x50"


def test_resize_updates_cols_rows(tmp_path):
    rec = _make_recorder(tmp_path)
    assert rec.cols == 80
    assert rec.rows == 24
    rec.resize(200, 60)
    assert rec.cols == 200
    assert rec.rows == 60
    rec.close()


# -- 5. close --------------------------------------------------------

def test_close_flushes_and_closes_file(tmp_path):
    rec = _make_recorder(tmp_path)
    rec.record_output(b"before-close")
    rec.close()

    # File is readable and complete after close
    lines = _read_lines(tmp_path / "test.cast")
    assert len(lines) == 2
    assert json.loads(lines[1])[2] == "before-close"

    # The underlying file handle is closed
    assert rec._fp.closed


def test_close_no_further_writes(tmp_path):
    rec = _make_recorder(tmp_path)
    rec.close()

    # Writing after close raises (ValueError for closed file)
    with pytest.raises(ValueError):
        rec.record_output(b"after-close")


# -- 6. UTF-8 incremental decoder -----------------------------------

def test_utf8_split_multibyte_emoji(tmp_path):
    """Split a 4-byte emoji across two record_output calls and verify
    the decoder reassembles it into a single clean string."""
    rec = _make_recorder(tmp_path)

    emoji = "\U0001f600"  # grinning face, 4 UTF-8 bytes: f0 9f 98 80
    raw = emoji.encode("utf-8")
    assert len(raw) == 4

    # Feed first 2 bytes, then the remaining 2
    rec.record_output(raw[:2])
    rec.record_output(raw[2:])
    rec.close()

    lines = _read_lines(tmp_path / "test.cast")
    # The first chunk (2 bytes) is incomplete — the incremental decoder
    # with 'replace' errors may emit nothing or a replacement char.
    # The important thing is that across all output events, the emoji
    # is properly reconstructed (or replaced gracefully) without
    # producing invalid JSON.
    output_events = [json.loads(l) for l in lines[1:]]
    combined = "".join(ev[2] for ev in output_events)
    # The emoji must appear intact in the combined output
    assert emoji in combined


def test_utf8_split_two_byte_sequence(tmp_path):
    """Split a 2-byte UTF-8 character across two calls."""
    rec = _make_recorder(tmp_path)

    char = "\u00e9"  # e-acute, 2 UTF-8 bytes: c3 a9
    raw = char.encode("utf-8")
    assert len(raw) == 2

    rec.record_output(raw[:1])
    rec.record_output(raw[1:])
    rec.close()

    lines = _read_lines(tmp_path / "test.cast")
    output_events = [json.loads(l) for l in lines[1:]]
    combined = "".join(ev[2] for ev in output_events)
    assert char in combined


def test_utf8_complete_sequence_not_split(tmp_path):
    """A complete multi-byte sequence in a single call works normally."""
    rec = _make_recorder(tmp_path)

    text = "caf\u00e9"
    rec.record_output(text.encode("utf-8"))
    rec.close()

    lines = _read_lines(tmp_path / "test.cast")
    assert len(lines) == 2
    assert json.loads(lines[1])[2] == text
