"""Unit tests for ``vncvt/oklch.py`` — OKLCH/Oklab colour-space conversions."""

from __future__ import annotations

import pytest

from vncvt.oklch import (
    generate_palette,
    oklab_to_srgb,
    oklch_to_srgb,
    srgb_to_oklab,
    srgb_to_oklch,
)


# ------------------------------------------------------------------
# 1. srgb ↔ oklab round-trip
# ------------------------------------------------------------------

_RGB_SAMPLES = [
    (0, 0, 0),
    (255, 255, 255),
    (128, 128, 128),
    (255, 0, 0),
    (0, 255, 0),
    (0, 0, 255),
    (255, 255, 0),
    (0, 255, 255),
    (255, 0, 255),
    (42, 99, 201),
    (200, 100, 50),
    (10, 10, 10),
    (245, 245, 245),
]


@pytest.mark.parametrize("rgb", _RGB_SAMPLES)
def test_srgb_oklab_round_trip(rgb: tuple[int, int, int]) -> None:
    """srgb → oklab → srgb should recover the original within ±1 per channel."""
    L, a, b = srgb_to_oklab(*rgb)
    r2, g2, b2 = oklab_to_srgb(L, a, b)
    assert abs(r2 - rgb[0]) <= 1, f"red channel: {r2} vs {rgb[0]}"
    assert abs(g2 - rgb[1]) <= 1, f"green channel: {g2} vs {rgb[1]}"
    assert abs(b2 - rgb[2]) <= 1, f"blue channel: {b2} vs {rgb[2]}"


# ------------------------------------------------------------------
# 2. srgb ↔ oklch round-trip
# ------------------------------------------------------------------

@pytest.mark.parametrize("rgb", _RGB_SAMPLES)
def test_srgb_oklch_round_trip(rgb: tuple[int, int, int]) -> None:
    """srgb → oklch → srgb should recover the original within ±1 per channel."""
    L, C, H = srgb_to_oklch(*rgb)
    r2, g2, b2 = oklch_to_srgb(L, C, H)
    assert abs(r2 - rgb[0]) <= 1, f"red channel: {r2} vs {rgb[0]}"
    assert abs(g2 - rgb[1]) <= 1, f"green channel: {g2} vs {rgb[1]}"
    assert abs(b2 - rgb[2]) <= 1, f"blue channel: {b2} vs {rgb[2]}"


# ------------------------------------------------------------------
# 3. Gamut clamping: out-of-range OKLCH inputs → valid sRGB 0–255
# ------------------------------------------------------------------

_OOR_LCH = [
    (2.0, 0.5, 0.0),       # L way above 1
    (-0.5, 0.3, 120.0),    # negative L
    (0.5, 1.0, 0.0),       # extreme chroma
    (0.5, 0.2, -45.0),     # negative hue
    (0.5, 0.2, 720.0),     # hue > 360
    (0.0, 0.0, 0.0),       # all zeros
    (1.0, 0.0, 0.0),       # max lightness, zero chroma
]


@pytest.mark.parametrize("lch", _OOR_LCH)
def test_gamut_clamping(lch: tuple[float, float, float]) -> None:
    """Out-of-range OKLCH inputs must still produce valid sRGB in 0-255."""
    r, g, b = oklch_to_srgb(*lch)
    assert 0 <= r <= 255, f"red out of range: {r}"
    assert 0 <= g <= 255, f"green out of range: {g}"
    assert 0 <= b <= 255, f"blue out of range: {b}"
    assert isinstance(r, int) and isinstance(g, int) and isinstance(b, int)


# ------------------------------------------------------------------
# 4. generate_palette: structure and value ranges
# ------------------------------------------------------------------

_EXPECTED_KEYS = {
    "black", "red", "green", "brown", "blue", "magenta", "cyan", "white",
    "brightblack", "brightred", "brightgreen", "brightyellow",
    "brightblue", "brightmagenta", "brightcyan", "brightwhite",
}


@pytest.mark.parametrize(
    "bg",
    [(0, 0, 0), (255, 255, 255), (30, 30, 50)],
    ids=["dark-bg", "light-bg", "custom-bg"],
)
def test_generate_palette_keys_and_types(bg: tuple[int, int, int]) -> None:
    """generate_palette returns 16 named ANSI keys, all (r,g,b) in 0-255."""
    pal = generate_palette(bg)
    assert set(pal.keys()) == _EXPECTED_KEYS
    for name, rgb in pal.items():
        assert isinstance(rgb, tuple), f"{name} is not a tuple"
        assert len(rgb) == 3, f"{name} has {len(rgb)} components"
        for i, ch in enumerate(rgb):
            assert isinstance(ch, int), f"{name}[{i}] is {type(ch).__name__}"
            assert 0 <= ch <= 255, f"{name}[{i}] = {ch} out of range"


def test_generate_palette_hue_bias() -> None:
    """hue_bias parameter produces a different (but still valid) palette."""
    default = generate_palette((0, 0, 0))
    warm = generate_palette((0, 0, 0), hue_bias="warm")
    cool = generate_palette((0, 0, 0), hue_bias="cool")
    # At least one chromatic slot should differ when hue bias is applied.
    assert warm != default, "warm bias should differ from default"
    assert cool != default, "cool bias should differ from default"


# ------------------------------------------------------------------
# 5. Known reference values: black → L≈0, white → L≈1
# ------------------------------------------------------------------

def test_black_oklab_lightness() -> None:
    """Pure black (0,0,0) should map to L approximately 0."""
    L, a, b = srgb_to_oklab(0, 0, 0)
    assert L == pytest.approx(0.0, abs=1e-6)


def test_white_oklab_lightness() -> None:
    """Pure white (255,255,255) should map to L approximately 1."""
    L, a, b = srgb_to_oklab(255, 255, 255)
    assert L == pytest.approx(1.0, abs=1e-3)


def test_black_oklch_lightness() -> None:
    """Pure black in OKLCH should have L≈0 and C≈0."""
    L, C, H = srgb_to_oklch(0, 0, 0)
    assert L == pytest.approx(0.0, abs=1e-6)
    assert C == pytest.approx(0.0, abs=1e-6)


def test_white_oklch_lightness() -> None:
    """Pure white in OKLCH should have L≈1 and C≈0 (achromatic)."""
    L, C, H = srgb_to_oklch(255, 255, 255)
    assert L == pytest.approx(1.0, abs=1e-3)
    assert C == pytest.approx(0.0, abs=1e-3)


def test_grey_has_near_zero_chroma() -> None:
    """A neutral grey should be essentially achromatic (C very small)."""
    L, C, H = srgb_to_oklch(128, 128, 128)
    assert C < 0.005, f"expected near-zero chroma for grey, got {C}"
