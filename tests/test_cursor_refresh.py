"""Unit test: Terminal.get_dirty_rows dirties the previous cursor row.

This is the "line refresh after Enter" cosmetic fix: when the cursor
moves between rows, the row it left must be added to the dirty set so
the next incremental render sends a repaint that erases the ghost
block on the client side. Non-incremental screenshots don't show the
bug (they always re-render everything), so this is a pure state test
on the Terminal class.
"""

from __future__ import annotations

import pytest

from vncvt.terminal import Terminal


@pytest.fixture
def terminal():
    """A Terminal backed by /bin/true so the PTY exits immediately;
    we only care about the in-process pyte screen + dirty tracking.
    (We can't avoid spawning a real subprocess because Terminal
    forks in __init__, but /bin/true is cheap.)"""
    term = Terminal(cols=80, rows=24, shell="/bin/true")
    yield term
    term.close()


def test_cursor_movement_dirties_previous_row(terminal):
    """When the cursor moves from one row to another, both the old
    and new rows show up in get_dirty_rows so the next render can
    erase the ghost block cursor on the old row."""
    # Drain the initial dirty set (pyte marks everything on startup).
    terminal.get_dirty_rows()

    # Move the cursor programmatically to simulate "was typing on
    # row 2, now moved to row 5".
    terminal.screen.cursor.x = 10
    terminal.screen.cursor.y = 2
    terminal.get_dirty_rows()  # flush the (0,0) -> (10,2) transition

    terminal.screen.cursor.y = 5
    dirty = terminal.get_dirty_rows()
    assert 2 in dirty, "previous cursor row (2) must be in dirty set"
    assert 5 in dirty, "new cursor row (5) must be in dirty set"


def test_no_dirty_row_when_cursor_stationary(terminal):
    """When the cursor doesn't move and nothing else changes, the
    dirty set is empty. This is the no-op baseline."""
    terminal.get_dirty_rows()  # drain initial
    dirty = terminal.get_dirty_rows()
    assert dirty == set(), f"expected empty dirty set, got {dirty}"


def test_cursor_row_is_tracked_across_calls(terminal):
    """After a move is reported, subsequent get_dirty_rows calls
    don't re-report the same transition."""
    terminal.get_dirty_rows()  # drain
    terminal.screen.cursor.y = 10
    first = terminal.get_dirty_rows()
    assert 10 in first

    second = terminal.get_dirty_rows()
    assert 10 not in second, (
        f"cursor row 10 was reported twice: second call saw {second}"
    )
