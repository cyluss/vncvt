"""Tests for color modes and phosphor palette tinting.

Three tiers of cost:
1. **Metadata tests** — all themes, no rendering. Check palette shape,
   chroma, hue invariants via apply_theme + palette sampling. Fast.
2. **Logic tests** — 3 representative themes (amber=warm, cyan=cool,
   light=inverted polarity). Prove ramp/dispatch/contrast via
   TerminalRenderer._resolve_color. Medium cost.
3. **Replay tests** — default theme only (amber). Full .cast replay +
   per-cell inspect. Proves end-to-end pipeline. Expensive.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vncvt.cast_replay import inspect_frame, replay_cast
from vncvt.palette import _STANDARD_PALETTE_256
from vncvt import palette as _palette_mod
from vncvt.renderer import TerminalRenderer
from vncvt.theme import apply_theme, THEMES

_ALL_THEMES = sorted(THEMES)
FIXTURE = Path(__file__).parent.parent / "fixtures" / "claude-light-row0-invisible.cast"

# 3 representative themes covering: warm hue, cool hue, inverted polarity.
_REPR_THEMES = ["amber", "cyan", "light"]


# ---- Tier 1: metadata tests (all themes, no rendering) ----


def test_standard_palette_256_shape():
    assert len(_STANDARD_PALETTE_256) == 256
    assert _STANDARD_PALETTE_256[9] == (255, 0, 0)
    assert _STANDARD_PALETTE_256[15] == (255, 255, 255)


@pytest.mark.parametrize("theme", _ALL_THEMES)
def test_phosphor_palette_shape(theme):
    """Every theme's phosphor palette must have exactly 256 entries."""
    apply_theme(theme)
    assert len(_palette_mod._PALETTE_PHOSPHOR) == 256


@pytest.mark.parametrize("theme", _ALL_THEMES)
def test_phosphor_palette_chroma(theme):
    """Chromatic themes must have visible chroma at mid-ramp (idx 241);
    neutral themes (dark/light) must stay grey. Cheap — no rendering."""
    from vncvt.oklch import srgb_to_oklch
    apply_theme(theme)
    idx241 = _palette_mod._PALETTE_PHOSPHOR[241]
    r, g, b = idx241
    chroma = max(r, g, b) - min(r, g, b)

    if chroma < 5:
        # Palette entry is neutral grey — only acceptable for themes
        # whose phosphor dict is intentionally achromatic (dark, light).
        # Themes like dos have white fg but blue-tinted phosphor dicts,
        # so we can't just check fg chroma.
        phosphor = THEMES[theme]["ansi_phosphor"]
        any_chromatic = any(
            max(v) - min(v) > 20 for v in phosphor.values()
        )
        assert not any_chromatic, (
            f"{theme} idx241={idx241} chroma={chroma} — grey, but the "
            f"phosphor dict has chromatic entries. Palette builder bug."
        )
    else:
        # Chromatic palette entry — verify the chroma floor scales
        # with the phosphor dict's peak saturation.
        phosphor = THEMES[theme]["ansi_phosphor"]
        peak = max(max(v) - min(v) for v in phosphor.values())
        floor = max(10, int(peak * 0.10))
        assert chroma >= floor, (
            f"{theme} idx241={idx241} chroma={chroma}, "
            f"expected >= {floor} (10% of phosphor peak chroma {peak})"
        )


# ---- Tier 2: logic tests (3 representative themes, light rendering) ----


@pytest.mark.parametrize("mode", ["phosphor", "true-color"])
@pytest.mark.parametrize("theme", _REPR_THEMES)
def test_color_mode_contrast(mode, theme):
    """Contrast floor (3.0:1) on representative themes × both modes."""
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


@pytest.mark.parametrize("theme", _REPR_THEMES)
def test_phosphor_pole_orientation(theme):
    """fg must be pushed AWAY from the cell's bg regardless of theme
    polarity. Dark cell_bg → bright fg; light cell_bg → dark fg."""
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


def test_phosphor_truecolor_hex_tinted_amber():
    """Truecolor green hex on amber must come out amber-tinted (the
    _apply_oklab_ramp OKLCH path, not the palette lookup path)."""
    ctx = apply_theme("amber")
    r = TerminalRenderer(cols=80, rows=24, theme=ctx, color_mode="phosphor")
    result = r._resolve_color("00c800", bold=False, is_bg=False, cell_bg=(0, 0, 0))
    chroma = max(result) - min(result)
    assert chroma >= 20, f"amber truecolor hex {result} chroma={chroma}"
    assert result[0] >= result[1] >= result[2], f"amber hue invariant violated: {result}"


def test_phosphor_truecolor_hex_tinted_cyan():
    """Truecolor green hex on cyan must come out cool-tinted."""
    ctx = apply_theme("cyan")
    r = TerminalRenderer(cols=80, rows=24, theme=ctx, color_mode="phosphor")
    result = r._resolve_color("00c800", bold=False, is_bg=False, cell_bg=(0, 0, 0))
    chroma = max(result) - min(result)
    assert chroma >= 10, f"cyan truecolor hex {result} chroma={chroma}"


# ---- Tier 3: full replay (default theme only) ----


def test_phosphor_collapses_hues_on_amber():
    """Full .cast replay on amber phosphor: no pixel should be
    blue-dominant. Proves the end-to-end pipeline works."""
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


def test_amber_row0_readable():
    """Full .cast replay on default theme: no invisible cells."""
    frame = replay_cast(FIXTURE, theme="amber")
    report = inspect_frame(frame, threshold=3.0)
    invisible = [
        c for c in report.below_threshold
        if c.char not in (" ", "\xa0")
    ]
    assert not invisible, f"amber: {invisible[:3]}"
