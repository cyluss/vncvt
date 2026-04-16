"""Theme definitions and the ``apply_theme()`` switcher for vncvt.

Each theme carries a background/foreground/bold/cursor color quad plus
a 16-slot phosphor palette (single-hue brightness ramp).  The active
theme's colors are exposed as module-level globals (``DEFAULT_BG``,
``DEFAULT_FG``, ``BOLD_FG``, ``CURSOR_COLOR``) for backward
compatibility, but new code should use :class:`ThemeContext` instead.

``apply_theme()`` builds a :class:`ThemeContext`, updates the legacy
module globals, and returns the context so callers can pass it to
:class:`~vncvt.renderer.TerminalRenderer`.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import palette as _palette_mod
from .palette import _build_256_phosphor, _PYTE_COLOR_NAMES

# ---------------------------------------------------------------------------
# Phosphor palettes -- single-hue per theme. Used by `phosphor` color
# mode (the default). Each palette has all 16 SGR slots but every slot
# is a *brightness* variant of the theme's primary hue -- the SGR
# index becomes a luminance cue, not a chroma cue. This is the VT220
# / MDA aesthetic: everything is amber (or green, or grey, or cyan).
#
# Slot 0 ("black") is *near-bg* by design, so it legitimately fails
# the 3:1 contrast floor the other 15 slots clear. See design-color.md.
# ---------------------------------------------------------------------------

# Amber phosphor -- preserved from the original vncvt release.
# Invariant: r >= g >= b (warmth).
_AMBER_PHOSPHOR = {
    "black":         (60, 40, 0),
    "red":           (255, 120, 60),
    "green":         (220, 200, 80),
    "brown":         (255, 220, 80),
    "blue":          (230, 180, 60),
    "magenta":       (255, 150, 100),
    "cyan":          (255, 210, 100),
    "white":         (255, 220, 140),
    "brightblack":   (180, 140, 60),
    "brightred":     (255, 160, 100),
    "brightgreen":   (255, 230, 120),
    "brightyellow":  (255, 240, 140),
    "brightblue":    (255, 200, 100),
    "brightmagenta": (255, 190, 140),
    "brightcyan":    (255, 230, 130),
    "brightwhite":   (255, 240, 180),
}

# Green phosphor -- preserved. Invariant: g >= {r, b}.
_GREEN_PHOSPHOR = {
    "black":         (0, 30, 10),
    "red":           (180, 255, 180),
    "green":         (60, 255, 120),
    "brown":         (120, 255, 80),
    "blue":          (40, 200, 100),
    "magenta":       (200, 255, 160),
    "cyan":          (100, 255, 180),
    "white":         (200, 255, 200),
    "brightblack":   (40, 180, 90),
    "brightred":     (220, 255, 200),
    "brightgreen":   (160, 255, 200),
    "brightyellow":  (200, 255, 100),
    "brightblue":    (80, 240, 140),
    "brightmagenta": (220, 255, 200),
    "brightcyan":    (180, 255, 220),
    "brightwhite":   (255, 255, 220),
}

# Light phosphor -- dark-grey ink-on-paper. Invariant: r == g == b.
# All slots clear WCAG 5:1+ on white bg.
_LIGHT_PHOSPHOR = {
    "black":         (0, 0, 0),
    "red":           (80, 80, 80),
    "green":         (70, 70, 70),
    "brown":         (60, 60, 60),
    "blue":          (50, 50, 50),
    "magenta":       (90, 90, 90),
    "cyan":          (75, 75, 75),
    "white":         (40, 40, 40),
    "brightblack":   (110, 110, 110),
    "brightred":     (65, 65, 65),
    "brightgreen":   (55, 55, 55),
    "brightyellow":  (45, 45, 45),
    "brightblue":    (35, 35, 35),
    "brightmagenta": (80, 80, 80),
    "brightcyan":    (60, 60, 60),
    "brightwhite":   (20, 20, 20),
}

# Dark phosphor -- light-grey VT100 aesthetic. Invariant: r == g == b.
# Slot 0 (black) is near-bg by design; other 15 clear WCAG 6:1+.
_DARK_PHOSPHOR = {
    "black":         (20, 20, 20),
    "red":           (180, 180, 180),
    "green":         (190, 190, 190),
    "brown":         (200, 200, 200),
    "blue":          (210, 210, 210),
    "magenta":       (170, 170, 170),
    "cyan":          (185, 185, 185),
    "white":         (220, 220, 220),
    "brightblack":   (140, 140, 140),
    "brightred":     (200, 200, 200),
    "brightgreen":   (215, 215, 215),
    "brightyellow":  (225, 225, 225),
    "brightblue":    (230, 230, 230),
    "brightmagenta": (195, 195, 195),
    "brightcyan":    (210, 210, 210),
    "brightwhite":   (255, 255, 255),
}

# C64 phosphor -- blue-purple family (Pepto NTSC palette lineage).
# Invariant: b >= r >= g (cool blue cast).
# Ramp from bg (53,40,121)=#352879 to fg (170,170,255)=#AAAAFF.
_C64_PHOSPHOR = {
    "black":         (53,  40, 121),  # near-bg
    "red":           (71,  60, 141),
    "green":         (82,  73, 155),
    "brown":         (94,  86, 168),
    "blue":          (65,  53, 134),
    "magenta":       (106, 99, 181),
    "cyan":          (117,112, 195),
    "white":         (129,125, 208),
    "brightblack":   (88,  79, 161),
    "brightred":     (141,138, 221),
    "brightgreen":   (152,151, 235),
    "brightyellow":  (158,157, 241),
    "brightblue":    (111,105, 188),
    "brightmagenta": (164,164, 248),
    "brightcyan":    (167,167, 252),
    "brightwhite":   (170,170, 255),
}

# DOS/CGA phosphor -- cool-white family on CGA dark blue #0000AA.
# Invariant: b >= r = g (neutral cool tint toward blue).
# Ramp from bg (0,0,170)=#0000AA to fg (255,255,255)=#FFFFFF.
_DOS_PHOSPHOR = {
    "black":         (0,   0,  170),  # near-bg
    "red":           (26,  26, 179),
    "green":         (51,  51, 187),
    "brown":         (77,  77, 196),
    "blue":          (13,  13, 174),
    "magenta":       (102,102, 204),
    "cyan":          (128,128, 213),
    "white":         (153,153, 221),
    "brightblack":   (64,  64, 191),
    "brightred":     (179,179, 230),
    "brightgreen":   (204,204, 238),
    "brightyellow":  (217,217, 242),
    "brightblue":    (115,115, 208),
    "brightmagenta": (230,230, 247),
    "brightcyan":    (242,242, 251),
    "brightwhite":   (255,255, 255),
}

# ---------------------------------------------------------------------------
# GTIA phosphor generator — builds a 16-slot single-hue phosphor dict
# from a black background to the given fg color. Each SGR slot is a
# different intensity along the ramp. Used by all Atari/GTIA-derived
# themes so they don't each need a hand-tuned dict.
# ---------------------------------------------------------------------------

def _make_gtia_phosphor(
    fg: tuple[int, int, int],
) -> dict[str, tuple[int, int, int]]:
    """Generate a 16-slot phosphor palette as a linear ramp from
    black ``(0, 0, 0)`` to ``fg``.

    The intensity assignments follow the same convention as the
    hand-tuned _AMBER_PHOSPHOR: black and blue are near-bg (dark),
    white is mid-bright, brightwhite = peak fg.
    """
    # t values: how far along the (0,0,0)→fg ramp each slot sits.
    # Ordered to match SGR conventions (black=darkest, blue=very dark,
    # white=mid-high, brightwhite=peak).
    _SLOTS = [
        ("black",         0.00),
        ("red",           0.20),
        ("green",         0.30),
        ("brown",         0.40),
        ("blue",          0.10),
        ("magenta",       0.50),
        ("cyan",          0.60),
        ("white",         0.70),
        ("brightblack",   0.25),
        ("brightred",     0.80),
        ("brightgreen",   0.85),
        ("brightyellow",  0.90),
        ("brightblue",    0.35),
        ("brightmagenta", 0.95),
        ("brightcyan",    0.97),
        ("brightwhite",   1.00),
    ]
    return {
        name: (
            min(255, int(fg[0] * t)),
            min(255, int(fg[1] * t)),
            min(255, int(fg[2] * t)),
        )
        for name, t in _SLOTS
    }


def _bold_from_fg(fg: tuple[int, int, int]) -> tuple[int, int, int]:
    """Brighten fg by ~15% for the bold slot, clamped to 255."""
    return (
        min(255, int(fg[0] * 1.15)),
        min(255, int(fg[1] * 1.15)),
        min(255, int(fg[2] * 1.15)),
    )


# GTIA phosphor palettes — OKLCH-chroma-boosted to match amber's
# vividness (C ≈ 0.144). Original NTSC lum-6 values are pastel by
# hardware design; boosting makes them visually competitive with the
# hand-tuned amber/green themes. Hue preserved, only chroma raised.
_GTIA_YELLOW_PHOSPHOR   = _make_gtia_phosphor((234, 195,  71))  # hue 4
_GTIA_OLIVE_PHOSPHOR    = _make_gtia_phosphor((210, 214,  92))  # hue 5
_GTIA_MINT_PHOSPHOR     = _make_gtia_phosphor((135, 237, 160))  # hue 7
_GTIA_CYAN_PHOSPHOR     = _make_gtia_phosphor(( 60, 238, 209))  # hue 8
_GTIA_PURPLE_PHOSPHOR   = _make_gtia_phosphor((212, 169, 255))  # hue 11
_GTIA_ORCHID_PHOSPHOR   = _make_gtia_phosphor((236, 162, 250))  # hue 12
_GTIA_ROSE_PHOSPHOR     = _make_gtia_phosphor((253, 153, 221))  # hue 13
_GTIA_SALMON_PHOSPHOR   = _make_gtia_phosphor((255, 145, 181))  # hue 14

# Active palette -- apply_theme() replaces this dict in place so any
# code holding a reference (including the 256-color builders below)
# sees the new colors. AMBER_COLORS is kept as a back-compat alias
# and points at the *phosphor* palette since that's the default mode.
AMBER_COLORS: dict[str, tuple[int, int, int]] = dict(_AMBER_PHOSPHOR)

# Theme palettes. The active one is installed into the module-level
# DEFAULT_BG / DEFAULT_FG / BOLD_FG / CURSOR_COLOR constants by
# ``apply_theme()``; modules like test_padding.py that import
# DEFAULT_BG directly pick up whatever the current theme set.
THEMES: dict[str, dict] = {
    # Each theme has one 16-slot palette: `ansi_phosphor` (single-hue
    # ramp for `phosphor` mode). In `true-color` mode the standard
    # xterm 256-color palette is used instead -- no per-theme variant.
    "amber": {
        "bg":            (0, 0, 0),
        "fg":            (255, 190, 80),   # 12.73:1
        "bold":          (255, 220, 120),  # ~14.5:1
        "cursor":        (255, 190, 80),
        "ansi_phosphor": _AMBER_PHOSPHOR,
    },
    "light": {  # black on white -- 21.00:1 (WCAG max)
        "bg":            (255, 255, 255),
        "fg":            (0, 0, 0),
        "bold":          (0, 0, 0),
        "cursor":        (0, 0, 0),
        "ansi_phosphor": _LIGHT_PHOSPHOR,
    },
    "dark": {  # white on black -- 21.00:1 (WCAG max)
        "bg":            (0, 0, 0),
        "fg":            (255, 255, 255),
        "bold":          (255, 255, 255),
        "cursor":        (255, 255, 255),
        "ansi_phosphor": _DARK_PHOSPHOR,
    },
    "green": {  # P31 phosphor green -- 16.31:1
        "bg":            (0, 0, 0),
        "fg":            (120, 255, 120),
        "bold":          (180, 255, 180),
        "cursor":        (120, 255, 120),
        "ansi_phosphor": _GREEN_PHOSPHOR,
    },
    "c64": {  # Commodore 64 BASIC -- Pepto NTSC palette
        "bg":            (53, 40, 121),    # #352879
        "fg":            (170, 170, 255),  # #AAAAFF  4.94:1 AA
        "bold":          (200, 200, 255),  # #C8C8FF
        "cursor":        (170, 170, 255),
        "ansi_phosphor": _C64_PHOSPHOR,
    },
    "dos": {  # CGA dark blue -- WordPerfect / Norton Commander
        "bg":            (0, 0, 170),      # #0000AA
        "fg":            (255, 255, 255),  # #FFFFFF  16.94:1 AAA
        "bold":          (255, 255, 85),   # #FFFF55  CGA bright yellow
        "cursor":        (255, 255, 255),
        "ansi_phosphor": _DOS_PHOSPHOR,
    },
    # --- Atari GTIA hues (OKLCH-boosted to match amber's chroma) ---
    "yellow":  {  # GTIA hue 4 ($4C)
        "bg": (0,0,0), "fg": (234,195,71), "bold": _bold_from_fg((234,195,71)),
        "cursor": (234,195,71), "ansi_phosphor": _GTIA_YELLOW_PHOSPHOR,
    },
    "olive":   {  # GTIA hue 5 ($5C)
        "bg": (0,0,0), "fg": (210,214,92), "bold": _bold_from_fg((210,214,92)),
        "cursor": (210,214,92), "ansi_phosphor": _GTIA_OLIVE_PHOSPHOR,
    },
    "mint":    {  # GTIA hue 7 ($7C)
        "bg": (0,0,0), "fg": (135,237,160), "bold": _bold_from_fg((135,237,160)),
        "cursor": (135,237,160), "ansi_phosphor": _GTIA_MINT_PHOSPHOR,
    },
    "cyan":    {  # GTIA hue 8 ($8C)
        "bg": (0,0,0), "fg": (60,238,209), "bold": _bold_from_fg((60,238,209)),
        "cursor": (60,238,209), "ansi_phosphor": _GTIA_CYAN_PHOSPHOR,
    },
    "purple":  {  # GTIA hue 11 ($BC)
        "bg": (0,0,0), "fg": (212,169,255), "bold": _bold_from_fg((212,169,255)),
        "cursor": (212,169,255), "ansi_phosphor": _GTIA_PURPLE_PHOSPHOR,
    },
    "orchid":  {  # GTIA hue 12 ($CC)
        "bg": (0,0,0), "fg": (236,162,250), "bold": _bold_from_fg((236,162,250)),
        "cursor": (236,162,250), "ansi_phosphor": _GTIA_ORCHID_PHOSPHOR,
    },
    "rose":    {  # GTIA hue 13 ($DC)
        "bg": (0,0,0), "fg": (253,153,221), "bold": _bold_from_fg((253,153,221)),
        "cursor": (253,153,221), "ansi_phosphor": _GTIA_ROSE_PHOSPHOR,
    },
    "salmon":  {  # GTIA hue 14 ($EC)
        "bg": (0,0,0), "fg": (255,145,181), "bold": _bold_from_fg((255,145,181)),
        "cursor": (255,145,181), "ansi_phosphor": _GTIA_SALMON_PHOSPHOR,
    },
}

DEFAULT_BG = THEMES["amber"]["bg"]
DEFAULT_FG = THEMES["amber"]["fg"]
BOLD_FG = THEMES["amber"]["bold"]
CURSOR_COLOR = THEMES["amber"]["cursor"]

# Initialize the palette module's phosphor palette with the default
# (amber) theme so it's ready before any explicit apply_theme() call.
_palette_mod._PALETTE_PHOSPHOR = _build_256_phosphor(
    _AMBER_PHOSPHOR, theme_fg=THEMES["amber"]["fg"],
)


# ---------------------------------------------------------------------------
# ThemeContext — immutable snapshot of a fully-resolved theme
# ---------------------------------------------------------------------------

@dataclass
class ThemeContext:
    """Immutable snapshot of a fully-resolved theme.

    Holds every value that the renderer needs so it never has to read
    the mutable module-level globals.  Create via ``from_theme()`` or
    ``apply_theme()``; pass to ``TerminalRenderer(theme=ctx)``.
    """

    name: str
    bg: tuple[int, int, int]
    fg: tuple[int, int, int]
    bold: tuple[int, int, int]
    cursor: tuple[int, int, int]
    phosphor_palette: list[tuple[int, int, int]]  # 256-slot
    ansi_colors: dict[str, tuple[int, int, int]]   # 16 named ANSI colors

    @classmethod
    def from_theme(cls, name: str) -> "ThemeContext":
        """Build a ThemeContext from a named theme."""
        if name not in THEMES:
            raise ValueError(
                f"unknown theme {name!r}; expected one of {sorted(THEMES)}"
            )
        theme_palette = THEMES[name]
        return cls(
            name=name,
            bg=theme_palette["bg"],
            fg=theme_palette["fg"],
            bold=theme_palette["bold"],
            cursor=theme_palette["cursor"],
            phosphor_palette=_build_256_phosphor(
                theme_palette["ansi_phosphor"],
                theme_fg=theme_palette["fg"],
            ),
            # Own copy — not a reference to the global AMBER_COLORS dict
            ansi_colors=dict(theme_palette["ansi_phosphor"]),
        )


def apply_theme(name: str) -> ThemeContext:
    """Install one of the THEMES palettes as the module-level defaults.

    Builds a :class:`ThemeContext`, updates the legacy module globals
    (``DEFAULT_BG``, ``DEFAULT_FG``, ``BOLD_FG``, ``CURSOR_COLOR``,
    ``AMBER_COLORS``, and ``palette._PALETTE_PHOSPHOR``), and returns
    the context.  New code should pass the returned context to
    ``TerminalRenderer(theme=ctx)`` instead of relying on the globals.
    """
    ctx = ThemeContext.from_theme(name)

    global DEFAULT_BG, DEFAULT_FG, BOLD_FG, CURSOR_COLOR
    DEFAULT_BG = ctx.bg
    DEFAULT_FG = ctx.fg
    BOLD_FG = ctx.bold
    CURSOR_COLOR = ctx.cursor
    _palette_mod._PALETTE_PHOSPHOR = ctx.phosphor_palette
    AMBER_COLORS.clear()
    AMBER_COLORS.update(ctx.ansi_colors)

    return ctx
