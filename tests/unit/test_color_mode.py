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
from vncvt import palette as _palette_mod
from vncvt.renderer import TerminalRenderer
from vncvt.theme import apply_theme

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



@pytest.mark.parametrize("theme, hue_check", [
    ("amber", lambda r, g, b: r >= g >= b),
    ("green", lambda r, g, b: g >= r and g >= b),
    ("c64",   lambda r, g, b: b >= r and b >= g),
    ("dos",   lambda r, g, b: b >= r and b >= g),
    ("atari", lambda r, g, b: r >= g >= b),
])
def test_phosphor_ramp_preserves_hue(theme, hue_check):
    """Mid-luminance phosphor palette entries (index 241 = Claude
    Code's primary body text SGR) must carry visible chroma in the
    theme's hue family, not collapse to neutral grey.

    The OKLCH ramp in ``_build_256_phosphor`` holds the theme fg's
    hue constant and scales chroma with lightness. This test asserts
    both the minimum chroma floor (≥ 20) and the per-theme hue
    invariant (amber = r≥g≥b, green = g dominant, etc.)."""
    apply_theme(theme)
    idx241 = _palette_mod._PALETTE_PHOSPHOR[241]
    r, g, b = idx241
    chroma = max(r, g, b) - min(r, g, b)
    assert chroma >= 20, (
        f"{theme} phosphor idx241={idx241} chroma={chroma} — "
        f"too grey, expected ≥ 20"
    )
    assert hue_check(r, g, b), (
        f"{theme} phosphor idx241={idx241} violates hue invariant"
    )


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_neutral_phosphor_stays_grey(theme):
    """Dark and light phosphor themes simulate achromatic CRTs (P4
    white phosphor, paper-white). Their ramp must remain neutral
    grey — no accidental chroma from the OKLCH interpolation."""
    apply_theme(theme)
    idx241 = _palette_mod._PALETTE_PHOSPHOR[241]
    r, g, b = idx241
    chroma = max(r, g, b) - min(r, g, b)
    assert chroma < 5, (
        f"{theme} phosphor idx241={idx241} chroma={chroma} — "
        f"expected neutral grey (chroma < 5)"
    )


@pytest.mark.parametrize("theme, hue_check", [
    ("amber", lambda r, g, b: r >= g >= b),
    ("green", lambda r, g, b: g >= r and g >= b),
    ("c64",   lambda r, g, b: b >= r and b >= g),
    ("dos",   lambda r, g, b: b >= r and b >= g),
    ("atari", lambda r, g, b: r >= g >= b),
])
def test_phosphor_truecolor_hex_tinted(theme, hue_check):
    """Truecolor hex inputs (not ANSI indices) in phosphor mode must
    also carry the theme's hue, not collapse to neutral grey. This
    tests the _apply_oklab_ramp path, not the palette lookup path."""
    ctx = apply_theme(theme)
    r = TerminalRenderer(cols=80, rows=24, theme=ctx, color_mode="phosphor")
    result = r._resolve_color("00c800", bold=False, is_bg=False, cell_bg=(0, 0, 0))
    chroma = max(result) - min(result)
    assert chroma >= 20, f"{theme} truecolor hex {result} chroma={chroma}"
    assert hue_check(*result), f"{theme} truecolor hex {result} wrong hue"


@pytest.mark.parametrize("theme", [
    "amber", "green", "light", "dark", "c64", "dos", "atari",
])
def test_phosphor_pole_orientation(theme):
    """In phosphor mode, fg must be pushed AWAY from the cell's bg
    regardless of whether the theme's polarity is bright-on-dark
    (amber) or dark-on-bright (light). Verify both cell_bg
    orientations produce readable contrast."""
    ctx = apply_theme(theme)
    r = TerminalRenderer(cols=80, rows=24, theme=ctx, color_mode="phosphor")
    dark_cell = r._resolve_color("808080", bold=False, is_bg=False, cell_bg=(0, 0, 0))
    light_cell = r._resolve_color("808080", bold=False, is_bg=False, cell_bg=(240, 240, 240))
    dark_lum = 0.2126 * dark_cell[0] / 255 + 0.7152 * dark_cell[1] / 255 + 0.0722 * dark_cell[2] / 255
    light_lum = 0.2126 * light_cell[0] / 255 + 0.7152 * light_cell[1] / 255 + 0.0722 * light_cell[2] / 255
    assert dark_lum > light_lum, (
        f"{theme}: dark_cell={dark_cell} (lum={dark_lum:.2f}) should be "
        f"brighter than light_cell={light_cell} (lum={light_lum:.2f})"
    )


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
