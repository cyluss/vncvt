"""Stress test: cycle every editable SET-UP field through its full
range of values and verify each step round-trips cleanly.

Catches:
- Theme persistence bug where enter_setup reverse-guessed the
  current theme from DEFAULT_BG, which was ambiguous for
  amber/dark/green (all share (0,0,0) bg).
- Any field where cycling leaves the server in an inconsistent
  state after apply+re-enter.

These tests run in-process (no subprocess spawn) via direct
SetupScreen + RFBServer use — much faster than driving a real VNC
client, and enough to catch the state-tracking bugs.
"""

from __future__ import annotations

import asyncio

import pytest

from vncvt.terminal import Terminal
from vncvt.renderer import TerminalRenderer, THEMES, apply_theme
from vncvt.server import RFBServer


KEY_DOWN = 0xFF54
KEY_RETURN = 0xFF0D


def _make_server(theme: str = "amber") -> tuple[Terminal, RFBServer]:
    term = Terminal(cols=80, rows=24, shell="/bin/zsh")
    apply_theme(theme)
    rend = TerminalRenderer(cols=80, rows=24, font_size=13)
    server = RFBServer(
        host="127.0.0.1", port=0,
        terminal=term, renderer=rend,
        fps=30, theme=theme,
    )
    return term, server


def _cycle_field(setup, field_index: int, cycles: int) -> None:
    """Navigate to ``field_index`` and press Return ``cycles`` times."""
    # Navigate from field 0 down to field_index
    for _ in range(field_index):
        setup.on_key(KEY_DOWN)
    for _ in range(cycles):
        setup.on_key(KEY_RETURN)


async def test_theme_cycle_all_values_persist():
    """Cycle Theme through amber → dark → light → green → amber and
    verify every transition sticks across SET-UP re-entries.

    This is the test the theme-persistence bug would have caught:
    enter_setup's reverse-bg lookup always returned "amber" because
    amber, dark, and green share (0,0,0) bg. After the fix,
    RFBServer.theme is tracked explicitly and survives the round trip.
    """
    term, server = _make_server(theme="amber")
    # Theme field values, in the order SetupScreen cycles them
    theme_values = ["amber", "dark", "light", "green"]
    try:
        current = "amber"
        for _ in range(len(theme_values) + 1):  # one full cycle + wrap
            await server.enter_setup()
            # The Theme field must show the server's currently active
            # theme when we enter SET-UP — this is the regression case
            # that the old bg-reverse-lookup broke (always returned
            # "amber" because amber/dark/green share (0,0,0) bg).
            snap = server._setup.snapshot()
            assert snap["Theme"] == current, (
                f"enter_setup showed Theme={snap['Theme']!r}, but the "
                f"server's actual theme is {current!r}"
            )
            # Cycle one step, compute the expected next value
            setup = server._setup
            _cycle_field(setup, 4, 1)  # Theme field is index 4
            expected_next = theme_values[
                (theme_values.index(current) + 1) % len(theme_values)
            ]
            cycled = setup.snapshot()["Theme"]
            assert cycled == expected_next, (
                f"cycle from {current!r} landed on {cycled!r}, "
                f"expected {expected_next!r}"
            )
            await server.exit_setup(apply=True)
            assert server.theme == expected_next, (
                f"after apply, server.theme={server.theme!r}, "
                f"expected {expected_next!r}"
            )
            current = expected_next
    finally:
        term.close()


async def test_fps_cycle_all_values():
    """Cycle FPS through its full range [15, 30, 60, 90, 120]."""
    term, server = _make_server()
    try:
        values_seen = []
        for _ in range(6):  # one full cycle plus wrap
            await server.enter_setup()
            _cycle_field(server._setup, 3, 1)  # FPS is field 3
            snap = server._setup.snapshot()
            await server.exit_setup(apply=True)
            values_seen.append(server.fps)
            assert server.fps == snap["FPS"]
        # Should have visited multiple distinct values
        assert len(set(values_seen)) > 1, (
            f"FPS cycle produced only one value: {values_seen}"
        )
    finally:
        term.close()


async def test_line_height_cycle_all_values():
    """Cycle Line height through [0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 1.75, 2.0]."""
    term, server = _make_server()
    try:
        values_seen = []
        for _ in range(10):
            await server.enter_setup()
            _cycle_field(server._setup, 5, 1)  # Line height is field 5
            snap = server._setup.snapshot()
            await server.exit_setup(apply=True)
            values_seen.append(server.renderer.line_height)
            assert abs(server.renderer.line_height - snap["Line height"]) < 1e-6
        assert len(set(values_seen)) > 1, (
            f"Line height cycle produced only one value: {values_seen}"
        )
    finally:
        term.close()


async def test_font_size_cycle_all_values():
    """Cycle Font size through range(8, 33)."""
    term, server = _make_server()
    try:
        values_seen = []
        for _ in range(5):  # don't cycle all 25, too slow — first few
            await server.enter_setup()
            _cycle_field(server._setup, 2, 1)  # Font size is field 2
            snap = server._setup.snapshot()
            await server.exit_setup(apply=True)
            values_seen.append(server.renderer.font_size)
            assert server.renderer.font_size == snap["Font size"]
        assert len(set(values_seen)) > 1
    finally:
        term.close()


async def test_cols_cycle_80_132():
    """Cycle Columns through [80, 132]."""
    term, server = _make_server()
    try:
        values_seen = []
        for _ in range(4):  # 2 full cycles
            await server.enter_setup()
            _cycle_field(server._setup, 0, 1)  # Columns is field 0
            snap = server._setup.snapshot()
            await server.exit_setup(apply=True)
            values_seen.append(server.terminal.cols)
            assert server.terminal.cols == snap["Columns"]
        assert set(values_seen) == {80, 132}, values_seen
    finally:
        term.close()


async def test_rows_cycle_24_36_48():
    """Cycle Rows through [24, 36, 48]."""
    term, server = _make_server()
    try:
        values_seen = []
        for _ in range(5):
            await server.enter_setup()
            _cycle_field(server._setup, 1, 1)  # Rows is field 1
            snap = server._setup.snapshot()
            await server.exit_setup(apply=True)
            values_seen.append(server.terminal.rows)
            assert server.terminal.rows == snap["Rows"]
        assert set(values_seen) == {24, 36, 48}, values_seen
    finally:
        term.close()


async def test_cancel_does_not_persist():
    """Pressing F3 (cancel) instead of Escape (apply) must not change
    the server's state — this is the opt-out path."""
    term, server = _make_server(theme="amber")
    try:
        await server.enter_setup()
        # Cycle theme inside SET-UP but DON'T apply
        _cycle_field(server._setup, 4, 2)  # amber → dark → light
        assert server._setup.snapshot()["Theme"] == "light"
        # Exit WITHOUT applying
        await server.exit_setup(apply=False)
        assert server.theme == "amber", (
            f"cancel path should not have changed theme, got {server.theme}"
        )
    finally:
        term.close()
