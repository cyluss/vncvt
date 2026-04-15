"""Tests for the ``color_mode`` parameter wired through
``replay_cast`` (Track C of the luminous-booping-rainbow plan).

The four tiers are the Win95-display-properties-inspired set:

- ``monochrome``   — single-hue VT220 phosphor ramp, no palette
- ``16-color``     — theme-tinted ANSI 16 + nearest snap
- ``256-color``    — full theme OKLCH 256 palette (default)
- ``true-color``   — standard xterm palette + raw truecolor

Only ``true-color`` renders Claude Code's native palette; the
other three are theme-tinted at varying fidelity.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vncvt.cast_replay import inspect_frame, replay_cast
from vncvt.renderer import _STANDARD_PALETTE_256

FIXTURE = Path(__file__).parent.parent / "fixtures" / "claude-light-row0-invisible.cast"


@pytest.mark.parametrize(
    "mode", ["monochrome", "16-color", "256-color", "true-color"]
)
@pytest.mark.parametrize(
    "theme", ["light", "dark", "amber", "green", "powershell"]
)
def test_color_mode_contrast(mode, theme):
    """No matter which color_mode we pick, no non-space cell should
    fall below the 3.0:1 contrast floor on any theme. The chunky
    16-color tier stays readable via bg-aware snap — fg candidates
    are filtered to palette entries that clear WCAG 3:1 against the
    cell bg before the nearest-RGB match runs."""
    frame = replay_cast(FIXTURE, theme=theme, color_mode=mode)
    report = inspect_frame(frame, threshold=3.0)
    invisible = [
        c for c in report.below_threshold
        if c.char not in (" ", "\xa0")
    ]
    assert not invisible, (
        f"{theme}/{mode}: {len(invisible)} invisible cells, "
        f"worst: {invisible[:3]}"
    )


def test_standard_palette_256_shape():
    """Sanity: the standard xterm 256 palette has 256 entries and
    the well-known anchors are correct (red 9 = pure red, white 15)."""
    assert len(_STANDARD_PALETTE_256) == 256
    assert _STANDARD_PALETTE_256[9] == (255, 0, 0)
    assert _STANDARD_PALETTE_256[15] == (255, 255, 255)


def test_true_color_and_256color_differ_on_amber():
    """``true-color`` uses the standard xterm palette for ANSI input
    while ``256-color`` uses the theme's OKLCH-tinted 256 palette.
    On the amber theme these must produce visibly different output
    because the theme palette has been re-hue'd toward warm amber."""
    tc = replay_cast(FIXTURE, theme="amber", color_mode="true-color")
    tt = replay_cast(FIXTURE, theme="amber", color_mode="256-color")
    tc_bytes = tc.image.convert("RGB").tobytes()
    tt_bytes = tt.image.convert("RGB").tobytes()
    diff = sum(abs(a - b) for a, b in zip(tc_bytes, tt_bytes))
    assert diff > 1000, (
        f"true-color and 256-color renders should differ visibly on "
        f"amber, but sum-abs-diff is only {diff}"
    )


def test_monochrome_collapses_hues_on_amber():
    """Monochrome on the amber theme should map every cell through
    the amber bg→fg ramp, so no pixel should be blue-dominant.
    Claude Code emits some cool ANSI hues that the theme's OKLCH
    256-color palette renders with a slight blue tilt; under
    monochrome they must collapse onto the warm amber axis.

    Allow a small slack for AA edge artifacts."""
    frame = replay_cast(FIXTURE, theme="amber", color_mode="monochrome")
    raw = frame.image.convert("RGB").tobytes()
    blue_dominant = sum(
        1 for i in range(0, len(raw), 3)
        if raw[i + 2] > raw[i] and raw[i + 2] > raw[i + 1]
    )
    assert blue_dominant < 10, (
        f"monochrome amber should have no blue-dominant pixels, "
        f"but found {blue_dominant}"
    )
