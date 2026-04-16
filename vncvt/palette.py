"""Color palette builders for vncvt's phosphor and true-color modes.

Phosphor mode collapses every SGR index onto a single-hue brightness
ramp (the VT220/MDA aesthetic).  True-color mode uses the standard
xterm 256-color palette so TUIs render in their native colors.

``_PALETTE_PHOSPHOR`` is a module-level variable that
``theme.apply_theme()`` replaces whenever the active theme changes.
Code that needs the current phosphor palette must access it as
``palette._PALETTE_PHOSPHOR`` (via ``from . import palette`` or
``import vncvt.palette as palette``) so the rebinding is visible.
"""

from __future__ import annotations

# pyte uses these names for the 8 basic colors
_PYTE_COLOR_NAMES = [
    "black", "red", "green", "brown", "blue", "magenta", "cyan", "white",
]

# Map normal SGR name -> bright SGR name (used by phosphor builder)
_BRIGHT_MAP = {
    "black": "brightblack", "red": "brightred", "green": "brightgreen",
    "brown": "brightyellow", "blue": "brightblue", "magenta": "brightmagenta",
    "cyan": "brightcyan", "white": "brightwhite",
}


def _build_256_phosphor(
    ansi16: dict[str, tuple[int, int, int]],
    theme_fg: tuple[int, int, int] | None = None,
) -> list[tuple[int, int, int]]:
    """Build a phosphor-mode 256-slot palette.

    Slots 0-15: verbatim phosphor entries (already single-hue).
    Slots 16-231: xterm 6x6x6 cube collapsed onto the single-hue
      ramp.  Interpolation is in **OKLCH** with the theme's hue
      and chroma held constant, varying only lightness.  This keeps
      mid-luminance entries visibly tinted instead of the grey-ish
      mid-tones a linear-RGB ramp produces.
    Slots 232-255: same OKLCH interpolation for the greyscale ramp.

    ``theme_fg`` is the theme's DEFAULT_FG, used as the hue/chroma
    anchor for the ramp.  It carries more saturation than the
    phosphor dict's ``brightwhite`` entry (which is bleached toward
    white by design).  If None, falls back to ``brightwhite``.
    """
    from .oklch import srgb_to_oklch, oklch_to_srgb, srgb_to_oklab

    palette: list[tuple[int, int, int]] = []
    for name in _PYTE_COLOR_NAMES:
        palette.append(ansi16[name])
    for name in _PYTE_COLOR_NAMES:
        palette.append(ansi16[_BRIGHT_MAP[name]])

    black = ansi16["black"]
    # Use the theme's primary fg as the hue/chroma anchor — it's
    # typically the most saturated representative of the theme's hue
    # identity. Fall back to the most chromatic entry in the phosphor
    # dict when theme_fg is achromatic (e.g. dos theme has pure-white
    # fg but a blue-tinted phosphor dict).
    hue_ref = theme_fg if theme_fg is not None else ansi16["brightwhite"]
    _, ref_C, ref_H = srgb_to_oklch(*hue_ref)

    if ref_C < 0.02:
        # theme_fg is near-neutral — scan the phosphor dict for the
        # entry with the highest OKLCH chroma and use that as the
        # hue/chroma anchor instead.
        best_C, best_H = 0.0, 0.0
        for entry in ansi16.values():
            _, c, h = srgb_to_oklch(*entry)
            if c > best_C:
                best_C, best_H = c, h
        ref_C, ref_H = best_C, best_H

    black_L, _, _ = srgb_to_oklch(*black)

    # The lightness endpoint is still brightwhite (the phosphor's
    # peak brightness) even though we take hue/chroma from theme_fg.
    fg = ansi16["brightwhite"]
    fg_L, _, _ = srgb_to_oklch(*fg)

    def _lum_to_ramp(r: int, g: int, b: int) -> tuple[int, int, int]:
        """Map input luminance onto the OKLCH phosphor ramp."""
        L_in, _, _ = srgb_to_oklab(r, g, b)
        # Gamma lift so mid-luminance inputs land in the readable
        # midrange (matches _apply_oklab_ramp's 0.4 exponent).
        t = min(1.0, max(0.0, L_in)) ** 0.4
        # Interpolate L between the black and fg endpoints.
        out_L = black_L + (fg_L - black_L) * t
        # Keep the theme's hue and chroma constant; scale chroma
        # proportionally with lightness so near-black entries don't
        # over-saturate (which would clip in sRGB).
        out_C = ref_C * t
        return oklch_to_srgb(out_L, out_C, ref_H)

    levels = [0, 0x5f, 0x87, 0xaf, 0xd7, 0xff]
    for r in levels:
        for g in levels:
            for b in levels:
                palette.append(_lum_to_ramp(r, g, b))

    for i in range(24):
        t = i / 23.0
        out_L = black_L + (fg_L - black_L) * t
        out_C = ref_C * t
        palette.append(oklch_to_srgb(out_L, out_C, ref_H))
    return palette


# Standard xterm 256-color palette -- NOT theme-tinted. Used by
# true-color mode so TUIs emitting ANSI indices render in their
# native xterm colors instead of the theme's OKLCH palette.
_STANDARD_ANSI_16 = [
    (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
    (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
    (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
]


def _build_standard_256() -> list[tuple[int, int, int]]:
    palette = list(_STANDARD_ANSI_16)
    levels = [0, 95, 135, 175, 215, 255]
    for r in levels:
        for g in levels:
            for b in levels:
                palette.append((r, g, b))
    for i in range(24):
        v = 8 + i * 10
        palette.append((v, v, v))
    return palette


_STANDARD_PALETTE_256 = _build_standard_256()


# Module-level phosphor palette. theme.apply_theme() rebuilds this
# whenever the theme changes. true-color mode uses
# _STANDARD_PALETTE_256 instead.  Initialized to None here; theme.py
# sets it at import time once the default AMBER phosphor is available.
_PALETTE_PHOSPHOR: list[tuple[int, int, int]] | None = None
