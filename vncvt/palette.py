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
) -> list[tuple[int, int, int]]:
    """Build a phosphor-mode 256-slot palette.

    Slots 0-15: verbatim phosphor entries (already single-hue).
    Slots 16-231: xterm 6x6x6 cube collapsed onto the single-hue
      brightness ramp by mapping each entry's perceived luminance to
      the palette's black->brightwhite axis. RGB blending (as used by
      ``_build_256_from_ansi16``) can't be used here because high-B
      xterm cube entries would keep enough blue to be blue-dominant
      after the blend, violating the single-hue invariant.
    Slots 232-255: linear interpolation on the black->brightwhite ramp
      (same as ``_build_256_from_ansi16``).
    """
    palette: list[tuple[int, int, int]] = []
    for name in _PYTE_COLOR_NAMES:
        palette.append(ansi16[name])
    for name in _PYTE_COLOR_NAMES:
        palette.append(ansi16[_BRIGHT_MAP[name]])

    black = ansi16["black"]
    white = ansi16["brightwhite"]

    def _lum_to_ramp(r: int, g: int, b: int) -> tuple[int, int, int]:
        """Map sRGB luminance of (r,g,b) onto the black->white ramp."""
        lin = lambda v: (v / 255.0 / 12.92 if v / 255.0 <= 0.04045
                         else ((v / 255.0 + 0.055) / 1.055) ** 2.4)
        lum = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
        # Gamma lift (matches _apply_oklab_ramp's 0.4 exponent) so
        # mid-luminance inputs land in the readable midrange rather
        # than clustering near black.
        t = min(1.0, max(0.0, lum)) ** 0.4
        return (
            int(black[0] + (white[0] - black[0]) * t),
            int(black[1] + (white[1] - black[1]) * t),
            int(black[2] + (white[2] - black[2]) * t),
        )

    levels = [0, 0x5f, 0x87, 0xaf, 0xd7, 0xff]
    for r in levels:
        for g in levels:
            for b in levels:
                palette.append(_lum_to_ramp(r, g, b))

    for i in range(24):
        t = i / 23.0
        palette.append((
            int(black[0] + (white[0] - black[0]) * t),
            int(black[1] + (white[1] - black[1]) * t),
            int(black[2] + (white[2] - black[2]) * t),
        ))
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
