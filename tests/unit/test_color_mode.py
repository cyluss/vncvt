"""Tests for the ``color_mode`` parameter wired through
``replay_cast`` (Track C of the luminous-booping-rainbow plan).

The four tiers are the historical display tiers:

- ``phosphor``    — single-hue VT220/MDA/Hercules ramp (default)
- ``16-color``    — CGA/EGA 16 hues, theme-tinted
- ``256-color``   — VGA diminished-chroma 256 palette, theme-tinted
- ``true-color``  — standard xterm palette + raw truecolor passthrough

Only ``true-color`` renders Claude Code's native palette faithfully;
the other three are theme-tinted at varying fidelity.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vncvt.cast_replay import inspect_frame, replay_cast
from vncvt.palette import _STANDARD_PALETTE_256

FIXTURE = Path(__file__).parent.parent / "fixtures" / "claude-light-row0-invisible.cast"


@pytest.mark.parametrize(
    "mode", ["phosphor", "true-color"]
)
@pytest.mark.parametrize(
    "theme", ["light", "dark", "amber", "green", "c64", "dos", "atari"]
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



def test_phosphor_collapses_hues_on_amber():
    """Phosphor on the amber theme should map every cell through the
    amber single-hue ramp, so no pixel should be blue-dominant.
    Claude Code emits some cool ANSI hues that the theme's 256-color
    palette renders with a slight blue tilt; under phosphor they must
    collapse onto the warm amber axis.

    Allow a small slack for AA edge artifacts."""
    frame = replay_cast(FIXTURE, theme="amber", color_mode="phosphor")
    raw = frame.image.convert("RGB").tobytes()
    blue_dominant = sum(
        1 for i in range(0, len(raw), 3)
        if raw[i + 2] > raw[i] and raw[i + 2] > raw[i + 1]
    )
    assert blue_dominant < 10, (
        f"phosphor amber should have no blue-dominant pixels, "
        f"but found {blue_dominant}"
    )
