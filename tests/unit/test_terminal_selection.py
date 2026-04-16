"""Unit tests for Terminal text-selection methods.

Terminal.__init__ forks a PTY, so these tests require Linux (or WSL).
We spawn a real Terminal with /bin/sh, then write known text directly
to the pyte screen via ``term.stream.feed()`` so cell contents are
deterministic and independent of shell output timing.
"""

from __future__ import annotations

import sys

import pytest

# Terminal requires fork/pty — skip the entire module on non-Unix.
if sys.platform == "win32":
    pytest.skip("PTY-based Terminal requires Linux/macOS", allow_module_level=True)

from vncvt.terminal import Terminal


@pytest.fixture
def term():
    """Create a small Terminal, seed three rows of known text, then yield it.

    The pyte screen is 40 cols x 10 rows. After seeding, the buffer
    looks like:

        row 0: "Hello, world!               ..."  (13 visible chars)
        row 1: "Second line here            ..."  (16 visible chars)
        row 2: "Third line!                 ..."  (11 visible chars)

    All remaining cells are spaces.
    """
    t = Terminal(cols=40, rows=10, shell="/bin/sh")
    try:
        # Reset cursor to home and write known text.
        # CSI H = cursor home, then plain text + newlines.
        t.stream.feed("\x1b[H\x1b[2J")  # clear screen, cursor home
        t.stream.feed("Hello, world!")
        t.stream.feed("\x1b[2;1H")  # move to row 2 col 1 (1-indexed)
        t.stream.feed("Second line here")
        t.stream.feed("\x1b[3;1H")  # move to row 3 col 1
        t.stream.feed("Third line!")
        yield t
    finally:
        t.close()


# ---- 1. Basic single-row selection ----

def test_basic_selection(term):
    """Select cols 0..4 on row 0 → 'Hello'."""
    term.begin_selection(0, 0)
    term.update_selection(4, 0)
    text = term.get_selection_text()
    assert text == "Hello"


def test_basic_selection_end(term):
    """end_selection() also returns the selected text."""
    term.begin_selection(0, 0)
    term.update_selection(4, 0)
    text = term.end_selection()
    assert text == "Hello"


# ---- 2. Backwards (right-to-left) selection ----

def test_backwards_selection(term):
    """Begin at col 12, update to col 0 on row 0 → 'Hello, world!'."""
    term.begin_selection(12, 0)
    term.update_selection(0, 0)
    text = term.get_selection_text()
    assert text == "Hello, world!"


def test_backwards_selection_partial(term):
    """Begin at col 7, update to col 0 on row 0 → 'Hello, w'."""
    term.begin_selection(7, 0)
    term.update_selection(0, 0)
    text = term.get_selection_text()
    assert text == "Hello, w"


# ---- 3. Multi-row selection ----

def test_multirow_selection(term):
    """Select from (0,0) to (5,2) spanning rows 0-2."""
    term.begin_selection(0, 0)
    term.update_selection(5, 2)
    text = term.get_selection_text()
    lines = text.split("\n")
    assert len(lines) == 3
    # Row 0: from col 0 to end of line (rstripped)
    assert lines[0] == "Hello, world!"
    # Row 1: full row (rstripped)
    assert lines[1] == "Second line here"
    # Row 2: cols 0..5
    assert lines[2] == "Third"


# ---- 4. clear_selection resets state ----

def test_clear_selection(term):
    """After clear_selection(), anchor and head are None."""
    term.begin_selection(0, 0)
    term.update_selection(5, 0)
    assert term.selection_anchor is not None
    assert term.selection_head is not None

    term.clear_selection()
    assert term.selection_anchor is None
    assert term.selection_head is None


def test_clear_selection_returns_empty_text(term):
    """get_selection_text() returns '' after clear."""
    term.begin_selection(0, 0)
    term.update_selection(5, 0)
    term.clear_selection()
    assert term.get_selection_text() == ""


# ---- 5. selection_normalized returns sorted coordinates ----

def test_normalized_forward(term):
    """Forward selection: normalized order matches input order."""
    term.begin_selection(2, 1)
    term.update_selection(10, 3)
    norm = term.selection_normalized()
    assert norm == ((2, 1), (10, 3))


def test_normalized_backward(term):
    """Backward selection: normalized flips to reading order."""
    term.begin_selection(10, 3)
    term.update_selection(2, 1)
    norm = term.selection_normalized()
    assert norm == ((2, 1), (10, 3))


def test_normalized_same_row_backward(term):
    """Backward on same row: normalized flips columns."""
    term.begin_selection(8, 0)
    term.update_selection(2, 0)
    norm = term.selection_normalized()
    assert norm == ((2, 0), (8, 0))


def test_normalized_none_when_no_selection(term):
    """No selection → normalized returns None."""
    assert term.selection_normalized() is None


# ---- 6. Empty / zero-width selection ----

def test_empty_selection_same_point(term):
    """Begin and end at the same cell → single character."""
    term.begin_selection(0, 0)
    term.update_selection(0, 0)
    text = term.get_selection_text()
    # (0,0) to (0,0) selects exactly one cell: 'H'
    assert text == "H"


def test_empty_selection_on_blank_cell(term):
    """Selecting a single blank cell returns empty string (rstripped)."""
    # Col 30 on row 0 is a trailing space — rstrip removes it.
    term.begin_selection(30, 0)
    term.update_selection(30, 0)
    text = term.get_selection_text()
    assert text == ""
