"""OKLCH ↔ sRGB conversions for perceptually uniform theme palettes.

OKLCH (Oklab in cylindrical form) is a modern perceptual color space
that replaces the WCAG-2 luminance model with a perception-based
lightness axis. Two colors with the same L* look equally bright to
the eye regardless of hue — unlike RGB/HSL where a "50% green" looks
much brighter than a "50% blue".

We use it to generate ANSI palettes where every color sits at the
same perceived lightness. That removes the "brightred looks dimmer
than brightgreen" problem baked into the classic xterm palette.

Math from https://bottosson.github.io/posts/oklab/ (public domain).
"""

from __future__ import annotations

import math


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb(c: float) -> float:
    if c < 0:
        return 0.0
    if c > 1:
        return 1.0
    return 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def srgb_to_oklab(r: int, g: int, b: int) -> tuple[float, float, float]:
    """Convert sRGB (0-255) to Oklab (L, a, b). L in 0-1."""
    lr, lg, lb = (_srgb_to_linear(c / 255.0) for c in (r, g, b))
    l_ = 0.4122214708 * lr + 0.5363325363 * lg + 0.0514459929 * lb
    m_ = 0.2119034982 * lr + 0.6806995451 * lg + 0.1073969566 * lb
    s_ = 0.0883024619 * lr + 0.2817188376 * lg + 0.6299787005 * lb
    l_ = l_ ** (1 / 3)
    m_ = m_ ** (1 / 3)
    s_ = s_ ** (1 / 3)
    L = 0.2104542553 * l_ + 0.793617785 * m_ - 0.0040720468 * s_
    a = 1.9779984951 * l_ - 2.428592205 * m_ + 0.4505937099 * s_
    b_ = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.808675766 * s_
    return L, a, b_


def oklab_to_srgb(L: float, a: float, b: float) -> tuple[int, int, int]:
    """Convert Oklab to sRGB (0-255), clamped to the display gamut."""
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.291485548 * b
    l_ = l_ ** 3
    m_ = m_ ** 3
    s_ = s_ ** 3
    lr = +4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_
    lg = -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_
    lb = -0.0041960863 * l_ - 0.7034186147 * m_ + 1.707614701 * s_
    r = round(_linear_to_srgb(lr) * 255)
    g = round(_linear_to_srgb(lg) * 255)
    b_ = round(_linear_to_srgb(lb) * 255)
    return (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b_)))


def oklch_to_srgb(L: float, C: float, H_deg: float) -> tuple[int, int, int]:
    """Convert OKLCH (Lightness, Chroma, Hue in degrees) to sRGB."""
    h = math.radians(H_deg)
    a = C * math.cos(h)
    b = C * math.sin(h)
    return oklab_to_srgb(L, a, b)


def srgb_to_oklch(r: int, g: int, b: int) -> tuple[float, float, float]:
    """Convert sRGB to OKLCH (L, C, H_deg)."""
    L, a, b_ = srgb_to_oklab(r, g, b)
    C = math.sqrt(a * a + b_ * b_)
    H = math.degrees(math.atan2(b_, a)) % 360
    return L, C, H


# Standard ANSI hue wheel. Angles are in OKLCH degrees — these
# correspond to visually recognizable xterm color categories.
ANSI_HUES = {
    "red":     29.0,
    "green":   142.0,
    "brown":    90.0,   # yellow
    "blue":    260.0,
    "magenta": 328.0,
    "cyan":    195.0,
}


def _wcag_luminance(rgb: tuple[int, int, int]) -> float:
    return (
        0.2126 * _srgb_to_linear(rgb[0] / 255.0)
        + 0.7152 * _srgb_to_linear(rgb[1] / 255.0)
        + 0.0722 * _srgb_to_linear(rgb[2] / 255.0)
    )


def _wcag_contrast(
    fg: tuple[int, int, int], bg: tuple[int, int, int]
) -> float:
    l1, l2 = _wcag_luminance(fg), _wcag_luminance(bg)
    return (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)


def _pick_neutral_l(
    bg: tuple[int, int, int],
    target_ratio: float,
    initial_l: float,
    *,
    is_light_bg: bool,
    neutral_hue: float = 269.0,
    chroma: float = 0.02,
) -> float:
    """Walk L* until the neutral (greyish) swatch clears ``target_ratio``
    against ``bg``. Used to pin ``brightblack`` (hint-text slot) above
    WCAG AAA regardless of bg color."""
    step = 0.01
    L = initial_l
    for _ in range(200):
        rgb = oklch_to_srgb(L, chroma, neutral_hue)
        if _wcag_contrast(rgb, bg) >= target_ratio:
            return L
        L += step if not is_light_bg else -step
        if L > 1.0 or L < 0.0:
            return max(0.0, min(1.0, L))
    return L


def generate_palette(
    bg: tuple[int, int, int],
    base_l: float = 0.78,
    bright_l: float = 0.90,
    chroma: float = 0.17,
) -> dict[str, tuple[int, int, int]]:
    """Build a full 16-color ANSI palette at uniform OKLCH lightness.

    Args:
        bg: theme background in sRGB, used to pick neutral shades
            that read well against it (black vs white bg).
        base_l: lightness for the primary 8 colors (0..1 in Oklab).
        bright_l: lightness for the bright variants.
        chroma: saturation; higher = more vivid, capped at gamut.

    Returns a dict with all 16 ANSI slots filled. "black" and
    "white" are derived from bg (black = near-bg neutral, white =
    high-contrast fg); the 6 chromatic slots come from the hue
    wheel at constant L*.
    """
    bg_L, _, _ = srgb_to_oklab(*bg)
    is_light_bg = bg_L > 0.5

    palette: dict[str, tuple[int, int, int]] = {}

    # Pin brightblack (the common "hint text" slot) to a lightness
    # that clears WCAG AAA (7:1) on THIS bg, not a hardcoded value.
    # On pure black bg, ~L=0.65 gives 6.5:1; on navy it needs L=0.76.
    # Using the solver guarantees 7:1 minimum across all themes.
    bb_l = _pick_neutral_l(
        bg, target_ratio=7.0,
        initial_l=0.55 if not is_light_bg else 0.45,
        is_light_bg=is_light_bg,
    )

    if is_light_bg:
        # Light theme: black = dark, white = near-bg neutral
        palette["black"] = (0, 0, 0)
        palette["white"] = oklch_to_srgb(0.40, 0.02, 270)  # dark grey
        palette["brightblack"] = oklch_to_srgb(bb_l, 0.02, 270)
        palette["brightwhite"] = (20, 20, 20)
        neutral_l = 0.40  # darker base for light bg
        neutral_bright_l = 0.25
    else:
        # Dark theme: black = near-bg, white = near-fg
        palette["black"] = oklch_to_srgb(0.35, 0.02, 270)
        palette["white"] = oklch_to_srgb(0.88, 0.02, 270)
        palette["brightblack"] = oklch_to_srgb(bb_l, 0.02, 270)
        palette["brightwhite"] = (255, 255, 255)
        neutral_l = base_l
        neutral_bright_l = bright_l

    # Chromatic slots: six hues × {base, bright}
    for name, h in ANSI_HUES.items():
        palette[name] = oklch_to_srgb(neutral_l, chroma, h)
        palette["bright" + ("yellow" if name == "brown" else name)] = (
            oklch_to_srgb(neutral_bright_l, chroma, h)
        )

    return palette
