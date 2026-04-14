"""Unit tests for vncvt.setup_screen.SetupScreen widget."""

from __future__ import annotations

import pytest

from vncvt.setup_screen import SetupField, SetupScreen


# Key constants from the widget (X11 keysyms)
KEY_UP = 0xFF52
KEY_DOWN = 0xFF54
KEY_LEFT = 0xFF51
KEY_RIGHT = 0xFF53
KEY_RETURN = 0xFF0D


# -------- SetupField --------


def test_field_initial_value():
    f = SetupField("Columns", [80, 132], index=0)
    assert f.current() == 80


def test_field_cycle_wraps():
    f = SetupField("Columns", [80, 132], index=0)
    f.cycle(1)
    assert f.current() == 132
    f.cycle(1)
    assert f.current() == 80


def test_field_cycle_backwards():
    f = SetupField("Rows", [24, 36, 48], index=0)
    f.cycle(-1)
    assert f.current() == 48


def test_readonly_field_does_not_cycle():
    f = SetupField("Server", ["vncvt 0.1.0"], readonly=True)
    f.cycle(1)
    assert f.current() == "vncvt 0.1.0"


def test_empty_values_rejected():
    with pytest.raises(ValueError):
        SetupField("bad", [])


# -------- SetupScreen --------


def test_initial_render_contains_labels():
    s = SetupScreen(80, 24)
    text = "\n".join(s.screen.display)
    assert "Columns" in text
    assert "Rows" in text
    assert "Font size" in text
    assert "FPS" in text


def test_initial_values_from_dict():
    s = SetupScreen(80, 24, initial={"cols": 132, "rows": 24, "font_size": 14, "fps": 60})
    snap = s.snapshot()
    assert snap["Columns"] == 132
    assert snap["Font size"] == 14
    assert snap["FPS"] == 60


def test_navigation_wraps():
    s = SetupScreen(80, 24)
    assert s.selected == 0
    s.on_key(KEY_UP)  # wraps to last editable field
    assert s.selected == 3  # 4 editable fields, indices 0..3
    s.on_key(KEY_DOWN)
    assert s.selected == 0


def test_return_cycles_selected_field():
    s = SetupScreen(80, 24, initial={"cols": 80})
    assert s.snapshot()["Columns"] == 80
    s.on_key(KEY_RETURN)
    assert s.snapshot()["Columns"] == 132


def test_left_key_cycles_backwards():
    s = SetupScreen(80, 24, initial={"cols": 80})
    s.on_key(KEY_LEFT)
    assert s.snapshot()["Columns"] == 132


def test_selected_row_has_reverse_video():
    s = SetupScreen(80, 24)
    # Row 2 is the first editable field ("Columns")
    # Check that the selected row has reverse-video applied
    line = s.screen.buffer[2]
    # pyte tracks SGR attrs per cell; reverse is on the cell's `reverse` flag
    assert line[2].reverse, "selected row should have reverse-video"


def test_unselected_row_no_reverse_video():
    s = SetupScreen(80, 24)
    # Row 3 is "Rows" (not selected)
    line = s.screen.buffer[3]
    assert not line[2].reverse


def test_snapshot_returns_editable_only():
    s = SetupScreen(80, 24)
    snap = s.snapshot()
    assert set(snap.keys()) == {"Columns", "Rows", "Font size", "FPS"}
    assert "Server" not in snap
    assert "Clients" not in snap


def test_unknown_key_not_consumed():
    s = SetupScreen(80, 24)
    assert s.on_key(0x1234) is False
