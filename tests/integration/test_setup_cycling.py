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

import pytest

from vncvt.terminal import Terminal
from vncvt.renderer import TerminalRenderer, apply_theme
from vncvt.server import RFBServer


KEY_DOWN = 0xFF54
KEY_RETURN = 0xFF0D


# Mapping from SET-UP field label to (field_index, probe). The probe
# is a function that reads the corresponding value off the server so
# we can assert the snapshot applied. Indices come from
# vncvt/setup_screen.py's editable field order: Columns, Rows,
# Font size, FPS, Theme, Line height.
_FIELD_PROBES = {
    "Columns":     (0, lambda s: s.terminal.cols),
    "Rows":        (1, lambda s: s.terminal.rows),
    "Font size":   (2, lambda s: s.renderer.font_size),
    "FPS":         (3, lambda s: s.fps),
    "Theme":       (4, lambda s: s.theme),
    "Line height": (5, lambda s: s.renderer.line_height),
}


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


async def _cycle_once(server: RFBServer, field_index: int) -> dict:
    """Enter SET-UP, navigate to field_index, press Return once,
    apply + exit. Returns the snapshot of the SET-UP widget
    immediately before exit so tests can cross-check it against
    the server state."""
    await server.enter_setup()
    setup = server._setup
    assert setup is not None
    for _ in range(field_index):
        setup.on_key(KEY_DOWN)
    setup.on_key(KEY_RETURN)
    snap = setup.snapshot()
    await server.exit_setup(apply=True)
    return snap


@pytest.mark.parametrize(
    "field_label",
    ["Columns", "Rows", "Font size", "FPS", "Theme", "Line height"],
)
async def test_field_cycle_round_trips(field_label):
    """Cycle one SET-UP field repeatedly and verify each applied
    value matches the widget snapshot AND the next enter_setup sees
    the new state.

    Regression for the theme-persistence bug where enter_setup
    reverse-guessed the current theme from DEFAULT_BG and was
    ambiguous for amber/dark/green.
    """
    field_index, probe = _FIELD_PROBES[field_label]
    term, server = _make_server(theme="amber")
    try:
        values_seen = []
        # Cycle enough times to hit >1 distinct value for every field.
        # FPS has 5 values, Theme has 4, Line height has 8, so 6 cycles
        # is enough to touch at least 2 distinct entries on each.
        for _ in range(6):
            snap = await _cycle_once(server, field_index)
            server_value = probe(server)
            snap_value = snap[field_label]
            # Floats (Line height) need epsilon compare; everything
            # else is int or str.
            if isinstance(server_value, float):
                assert abs(server_value - snap_value) < 1e-6, (
                    f"{field_label}: server={server_value}, snap={snap_value}"
                )
            else:
                assert server_value == snap_value, (
                    f"{field_label}: server={server_value!r}, "
                    f"snap={snap_value!r}"
                )
            values_seen.append(server_value)

            # The next enter_setup must see the just-applied value,
            # not the initial one. (The theme-persistence bug fails
            # here: reverse-guessing from DEFAULT_BG returns "amber"
            # regardless of the actual applied theme.)
            await server.enter_setup()
            next_snap = server._setup.snapshot()
            await server.exit_setup(apply=False)
            next_val = next_snap[field_label]
            if isinstance(server_value, float):
                assert abs(next_val - server_value) < 1e-6, (
                    f"{field_label}: re-entered SET-UP showed "
                    f"{next_val}, but server state is {server_value}"
                )
            else:
                assert next_val == server_value, (
                    f"{field_label}: re-entered SET-UP showed "
                    f"{next_val!r}, but server state is {server_value!r}"
                )
        # Each field has at least 2 values, so cycling must visit
        # more than one.
        assert len(set(values_seen)) > 1, (
            f"{field_label} cycle produced only one distinct value: "
            f"{values_seen}"
        )
    finally:
        term.close()


async def test_cancel_does_not_persist():
    """Pressing F3 (cancel) instead of Escape (apply) must not change
    the server's state — this is the opt-out path."""
    term, server = _make_server(theme="amber")
    try:
        await server.enter_setup()
        setup = server._setup
        # Navigate to Theme (field 4) and cycle twice
        for _ in range(4):
            setup.on_key(KEY_DOWN)
        setup.on_key(KEY_RETURN)
        setup.on_key(KEY_RETURN)
        assert setup.snapshot()["Theme"] == "light"
        # Exit WITHOUT applying
        await server.exit_setup(apply=False)
        assert server.theme == "amber", (
            f"cancel path should not have changed theme, got {server.theme}"
        )
    finally:
        term.close()
