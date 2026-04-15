"""Terminal-to-pixel rendering with VT220 amber-tinted color scheme."""

import logging
import math
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
import pyte
import skia
import wcwidth

log = logging.getLogger(__name__)


# Per-theme 16-color ANSI palettes. The active palette is rebound to
# AMBER_COLORS by apply_theme() so the existing _resolve_color path
# (which looks up by name in AMBER_COLORS) picks up theme changes.
_AMBER_ANSI = {
    "black":         (61, 40, 0),
    "red":           (255, 92, 0),
    "green":         (204, 163, 0),
    "brown":         (255, 200, 40),    # ANSI yellow — brighter amber
    "blue":          (204, 128, 0),
    "magenta":       (255, 122, 61),
    "cyan":          (230, 184, 0),
    "white":         (255, 204, 102),
    "brightblack":   (102, 72, 10),
    "brightred":     (255, 140, 50),
    "brightgreen":   (255, 210, 50),
    "brightyellow":  (255, 240, 100),   # bright yellow — punchy
    "brightblue":    (230, 170, 50),
    "brightmagenta": (255, 170, 100),
    "brightcyan":    (255, 220, 80),
    "brightwhite":   (255, 240, 170),
}

# Standard xterm-style palette for non-amber themes.
_STD_ANSI = {
    "black":         (0, 0, 0),
    "red":           (205, 49, 49),
    "green":         (13, 188, 121),
    "brown":         (229, 229, 16),    # ANSI yellow
    "blue":          (36, 114, 200),
    "magenta":       (188, 63, 188),
    "cyan":          (17, 168, 205),
    "white":         (229, 229, 229),
    "brightblack":   (102, 102, 102),
    "brightred":     (241, 76, 76),
    "brightgreen":   (35, 209, 139),
    "brightyellow":  (245, 245, 67),
    "brightblue":    (59, 142, 234),
    "brightmagenta": (214, 112, 214),
    "brightcyan":    (41, 184, 219),
    "brightwhite":   (255, 255, 255),
}

# Light theme uses standard ANSI but swaps a few high-luminance shades
# down so they're visible on a white background.
_LIGHT_ANSI = {
    **_STD_ANSI,
    "white":         (90, 90, 90),
    "brightwhite":   (60, 60, 60),
}

# PowerShell classic — Windows PowerShell's default color scheme.
# bg = #012456 (dark navy), fg = #EEEDF0 (off-white). Same standard
# xterm ANSI colors as our "dark" theme since PowerShell uses
# standard Windows console colors.
_POWERSHELL_ANSI = dict(_STD_ANSI)

# Green phosphor theme — every ANSI color becomes a shade of green.
_GREEN_ANSI = {
    "black":         (0, 30, 10),
    "red":           (180, 255, 180),
    "green":         (60, 255, 120),
    "brown":         (120, 255, 80),
    "blue":          (40, 200, 100),
    "magenta":       (200, 255, 160),
    "cyan":          (100, 255, 180),
    "white":         (200, 255, 200),
    "brightblack":   (40, 100, 40),
    "brightred":     (220, 255, 200),
    "brightgreen":   (160, 255, 200),
    "brightyellow":  (200, 255, 100),
    "brightblue":    (80, 240, 140),
    "brightmagenta": (220, 255, 200),
    "brightcyan":    (180, 255, 220),
    "brightwhite":   (255, 255, 220),
}

# Active palette — apply_theme() replaces this dict in place so any
# code holding a reference (including _build_256_palette below) sees
# the new colors.
AMBER_COLORS = dict(_AMBER_ANSI)

# pyte uses these names for the 8 basic colors
_PYTE_COLOR_NAMES = [
    "black", "red", "green", "brown", "blue", "magenta", "cyan", "white",
]

# Chars that should NEVER get an underline drawn under them, even if
# the cell has underscore=True. Covers ASCII space, NBSP, and the
# whole Unicode box-drawing block so separator rows don't produce a
# visible dark band.
_NO_UNDERLINE = frozenset(
    [" ", "\xa0"] + [chr(c) for c in range(0x2500, 0x2580)]
)

# Theme palettes. The active one is installed into the module-level
# DEFAULT_BG / DEFAULT_FG / BOLD_FG / CURSOR_COLOR constants by
# ``apply_theme()``; modules like test_padding.py that import
# DEFAULT_BG directly pick up whatever the current theme set.
THEMES: dict[str, dict] = {
    # All themes clear WCAG AAA (7:1); light/dark are at the 21:1 max.
    # Amber and green are capped by the need to preserve hue identity
    # — pushing a saturated hue past ~16:1 starts bleaching it toward
    # yellow or mint, which is neither amber nor phosphor green.
    "amber": {
        "bg":     (0, 0, 0),
        "fg":     (255, 190, 80),  # 12.73:1, warmer/orangy amber
        "bold":   (255, 220, 120), # ~14.5:1, brighter peach-amber for bold
        "cursor": (255, 190, 80),
        "ansi":   _AMBER_ANSI,
    },
    "light": {  # black on white — 21.00:1 (WCAG max)
        "bg":     (255, 255, 255),
        "fg":     (0, 0, 0),
        "bold":   (0, 0, 0),
        "cursor": (0, 0, 0),
        "ansi":   _LIGHT_ANSI,
    },
    "dark": {  # pure white on black — 21.00:1 (WCAG max)
        "bg":     (0, 0, 0),
        "fg":     (255, 255, 255),
        "bold":   (255, 255, 255),
        "cursor": (255, 255, 255),
        "ansi":   _STD_ANSI,
    },
    "green": {  # bright phosphor green — 16.31:1, keeps green hue
        "bg":     (0, 0, 0),
        "fg":     (120, 255, 120),
        "bold":   (180, 255, 180),
        "cursor": (120, 255, 120),
        "ansi":   _GREEN_ANSI,
    },
    "powershell": {  # Windows PowerShell classic — 12.97:1
        "bg":     (0x01, 0x24, 0x56),  # #012456 dark navy
        "fg":     (0xEE, 0xED, 0xF0),  # #EEEDF0 off-white
        "bold":   (0xFF, 0xFF, 0xFF),  # pure white for bold
        "cursor": (0xEE, 0xED, 0xF0),
        "ansi":   _POWERSHELL_ANSI,
    },
}

DEFAULT_BG = THEMES["amber"]["bg"]
DEFAULT_FG = THEMES["amber"]["fg"]
BOLD_FG = THEMES["amber"]["bold"]
CURSOR_COLOR = THEMES["amber"]["cursor"]


def apply_theme(name: str) -> None:
    """Install one of the THEMES palettes as the module-level defaults.

    Call before constructing any TerminalRenderer — the render path
    reads DEFAULT_BG / DEFAULT_FG / BOLD_FG / CURSOR_COLOR at draw
    time, so swapping them here is enough for a theme change. The
    16-color ANSI palette is updated in place so existing references
    in _PALETTE_256 stay current.
    """
    if name not in THEMES:
        raise ValueError(
            f"unknown theme {name!r}; expected one of {sorted(THEMES)}"
        )
    global DEFAULT_BG, DEFAULT_FG, BOLD_FG, CURSOR_COLOR, _PALETTE_256
    palette = THEMES[name]
    DEFAULT_BG = palette["bg"]
    DEFAULT_FG = palette["fg"]
    BOLD_FG = palette["bold"]
    CURSOR_COLOR = palette["cursor"]
    AMBER_COLORS.clear()
    AMBER_COLORS.update(palette["ansi"])
    # 256-color palette is built from AMBER_COLORS, so rebuild it
    _PALETTE_256 = _build_256_palette()

_VENDORED_FONT_DIR = Path(__file__).parent / "fonts"

FONT_SEARCH_PATHS = [
    # macOS: SF Mono Regular (system font). With Skia's subpixel AA
    # and LCD filtering, this matches what macOS Terminal.app renders.
    "/System/Library/Fonts/SFNSMono.ttf",
    # macOS fallback
    "/System/Library/Fonts/Menlo.ttc",
    # Linux fallbacks
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf",
    # Vendored Terminus TTF is kept as the ultimate fallback for
    # environments without any monospace font installed.
    str(_VENDORED_FONT_DIR / "TerminusTTF-4.49.3.ttf"),
]

BOLD_FONT_SEARCH_PATHS = [
    "/System/Library/Fonts/SFNSMono.ttf",  # has an embedded Bold face
    "/System/Library/Fonts/Menlo.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeMonoBold.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf",
    str(_VENDORED_FONT_DIR / "TerminusTTF-Bold-4.49.3.ttf"),
]


def _find_font(paths: list[str]) -> str | None:
    for path in paths:
        if Path(path).exists():
            return path
    if shutil.which("fc-match"):
        try:
            out = subprocess.check_output(
                ["fc-match", "-f", "%{file}", "monospace"], text=True
            ).strip()
            if Path(out).exists():
                return out
        except subprocess.SubprocessError:
            pass
    return None


# 256-color palette (indices 0-255) mapped to amber-tinted RGB.
# 0-7: standard, 8-15: bright, 16-231: 6x6x6 color cube, 232-255: grayscale
def _build_256_palette() -> list[tuple[int, int, int]]:
    palette: list[tuple[int, int, int]] = []
    # 0-7 standard
    for name in _PYTE_COLOR_NAMES:
        palette.append(AMBER_COLORS[name])
    # 8-15 bright
    _BRIGHT_MAP = {
        "black": "brightblack", "red": "brightred", "green": "brightgreen",
        "brown": "brightyellow", "blue": "brightblue", "magenta": "brightmagenta",
        "cyan": "brightcyan", "white": "brightwhite",
    }
    for name in _PYTE_COLOR_NAMES:
        palette.append(AMBER_COLORS[_BRIGHT_MAP[name]])
    # 16-231: 6x6x6 color cube — amber tint by warming each color
    _levels = [0, 0x5f, 0x87, 0xaf, 0xd7, 0xff]
    for r_idx in range(6):
        for g_idx in range(6):
            for b_idx in range(6):
                r, g, b = _levels[r_idx], _levels[g_idx], _levels[b_idx]
                # Compute luminance, then map to amber intensity
                lum = (r * 299 + g * 587 + b * 114) / 1000
                # Blend: keep some original hue, shift toward amber
                ar = int(r * 0.4 + lum * 0.6 * 255 / 255)
                ag = int(g * 0.2 + lum * 0.5 * 156 / 255)
                ab = int(b * 0.1 + lum * 0.1 * 50 / 255)
                palette.append((min(ar, 255), min(ag, 255), min(ab, 255)))
    # 232-255: grayscale ramp — map to amber intensity ramp
    for i in range(24):
        v = 8 + i * 10  # 8 to 238
        ratio = v / 255
        palette.append((
            int(255 * ratio),
            int(156 * ratio),
            int(0),
        ))
    return palette


_PALETTE_256 = _build_256_palette()


def _cell_in_selection(
    col: int,
    row: int,
    sel: tuple[tuple[int, int], tuple[int, int]] | None,
) -> bool:
    """Return True if (col, row) lies within the normalized selection."""
    if sel is None:
        return False
    (sc, sr), (ec, er) = sel
    if row < sr or row > er:
        return False
    if row == sr and row == er:
        return sc <= col <= ec
    if row == sr:
        return col >= sc
    if row == er:
        return col <= ec
    return True


class TerminalRenderer:
    """Renders a pyte Screen to an RGBX pixel buffer with amber-tinted colors."""

    # Overscan compensation: extra pixels of background around the
    # cell grid, like a CRT's overscan region. Mostly cosmetic — it
    # keeps the first column of text from hugging the screen edge.
    PADDING = 5

    def __init__(
        self,
        cols: int = 80,
        rows: int = 24,
        font_path: str | None = None,
        font_size: int = 16,
        line_height: float = 1.0,
    ):
        self.cols = cols
        self.rows = rows
        self.padding = self.PADDING
        self.line_height = line_height

        # Load fonts
        if font_path is None:
            font_path = _find_font(FONT_SEARCH_PATHS)
        if font_path is None:
            raise RuntimeError(
                "No monospace font found. Install dejavu-sans-mono or use --font."
            )
        # Stash resolved font path + size so Phase 2 SET-UP mode can
        # rebuild this renderer without guessing (Pillow's ImageFont
        # doesn't expose the path portably across versions).
        self._font_path = font_path
        self.font_size = font_size
        self.font = ImageFont.truetype(font_path, font_size)

        bold_path = _find_font(BOLD_FONT_SEARCH_PATHS)
        self.font_bold = (
            ImageFont.truetype(bold_path, font_size) if bold_path else self.font
        )

        # Skia fonts for actual glyph rasterization. Pillow's fonts
        # above are kept for getlength() / getmetrics() measurement
        # only — Skia does the drawing so we get subpixel positioning,
        # LCD filtering, and stem darkening that Pillow+FreeType doesn't
        # expose.
        self._skia_typeface = skia.Typeface.MakeFromFile(font_path)
        self._skia_typeface_bold = (
            skia.Typeface.MakeFromFile(bold_path) if bold_path
            else self._skia_typeface
        )
        self._skia_font = skia.Font(self._skia_typeface, font_size)
        self._skia_font_bold = skia.Font(self._skia_typeface_bold, font_size)

        # Font fallback chain. When a codepoint's glyph is missing from
        # the primary typeface (Hangul / CJK / emoji), we walk this list
        # and pick the first entry whose unicharToGlyph returns non-zero.
        # Order matters: we want SF Mono (or whatever --font was set)
        # to win for Latin, Sarasa Mono K for CJK, and Apple Color
        # Emoji for pictographic codepoints.
        self._skia_fallback_fonts: list[skia.Font] = [self._skia_font]
        self._skia_fallback_fonts_bold: list[skia.Font] = [self._skia_font_bold]
        # Per-font "is color bitmap" flag so _draw_glyph can avoid
        # tinting emoji via Paint.setColor.
        self._skia_fallback_is_color: list[bool] = [False]
        self._skia_fallback_is_color_bold: list[bool] = [False]

        sarasa_reg = _VENDORED_FONT_DIR / "SarasaMonoK-Regular.ttf"
        sarasa_bold = _VENDORED_FONT_DIR / "SarasaMonoK-Bold.ttf"
        if sarasa_reg.is_file():
            tf = skia.Typeface.MakeFromFile(str(sarasa_reg))
            if tf is not None:
                self._skia_fallback_fonts.append(skia.Font(tf, font_size))
                self._skia_fallback_is_color.append(False)
        if sarasa_bold.is_file():
            tf = skia.Typeface.MakeFromFile(str(sarasa_bold))
            if tf is not None:
                self._skia_fallback_fonts_bold.append(skia.Font(tf, font_size))
                self._skia_fallback_is_color_bold.append(False)

        apple_emoji = Path("/System/Library/Fonts/Apple Color Emoji.ttc")
        if apple_emoji.is_file():
            tf = skia.Typeface.MakeFromFile(str(apple_emoji))
            if tf is not None:
                emoji_font = skia.Font(tf, font_size)
                self._skia_fallback_fonts.append(emoji_font)
                self._skia_fallback_is_color.append(True)
                # Bold reuses the same emoji font — color emoji has no
                # weight axis.
                self._skia_fallback_fonts_bold.append(emoji_font)
                self._skia_fallback_is_color_bold.append(True)

        # Apply shared rasterization flags to every fallback font.
        for f in (
            *self._skia_fallback_fonts,
            *self._skia_fallback_fonts_bold,
        ):
            f.setSubpixel(True)
            # Grayscale AA (not LCD subpixel) — amber-on-black has no
            # blue channel transition, so subpixel rendering produces
            # red/green fringes that hurt perceived crispness. Plain
            # gray AA stays out of the way.
            f.setEdging(skia.Font.Edging.kAntiAlias)
            f.setHinting(skia.FontHinting.kFull)
            # Let SF Mono's native TrueType hints drive — they're
            # hand-tuned by Apple and beat Skia's autohinter.
            f.setForceAutoHinting(False)
            f.setLinearMetrics(False)
            f.setBaselineSnap(True)

        # Baseline offset: Pillow's draw.text anchors glyphs at the
        # top-left; Skia's drawString anchors at the baseline. Stash
        # the font's ascent so the call site can pass the baseline
        # y-coordinate.
        skia_metrics = self._skia_font.getMetrics()
        self._skia_baseline = -skia_metrics.fAscent

        # Measure character cell using advance width, not ink bbox.
        # getlength() returns the horizontal advance — the correct metric for
        # grid layout in a monospace font. Use math.ceil so we always
        # round UP; int(round()) can undersize the cell by ~0.5 px for
        # fonts whose advance is fractional (SF Mono at 13pt → 7.8,
        # rounded to 8, which leaves a sub-pixel gap every ~6 glyphs).
        self.cell_width = math.ceil(self.font.getlength("M"))
        ascent, descent = self.font.getmetrics()
        font_cell_h = ascent + descent
        self.cell_height = max(1, math.ceil(font_cell_h * self.line_height))
        self._x_offset = 0
        # Vertically center the glyph inside the expanded cell when
        # line_height > 1.0 so the extra space is shared above/below.
        self._y_offset = (self.cell_height - font_cell_h) // 2
        self._skia_baseline += self._y_offset

        # Warn if bold font has a different advance (would cause grid drift).
        if self.font_bold is not self.font:
            bold_adv = int(round(self.font_bold.getlength("M")))
            if bold_adv != self.cell_width:
                log.warning(
                    "Bold font advance (%d) != regular (%d); bold glyphs "
                    "will be clipped to the regular cell width",
                    bold_adv, self.cell_width,
                )

        self.width = cols * self.cell_width + 2 * self.padding
        self.height = rows * self.cell_height + 2 * self.padding

        # Create framebuffer image (padded by DEFAULT_BG on all sides).
        self.image = Image.new("RGBX", (self.width, self.height), DEFAULT_BG)
        self._prev_cursor = (-1, -1)

    def _find_font_for_char(
        self, ch: str, bold: bool,
    ) -> tuple["skia.Font", bool]:
        """Pick the first fallback font that has a glyph for ``ch``.

        Returns ``(font, is_color)`` where is_color signals that the
        font renders via color bitmaps (Apple Color Emoji) and the
        caller must not apply a Paint color — that would tint the
        sbix bitmap.
        """
        if bold:
            fonts = self._skia_fallback_fonts_bold
            color_flags = self._skia_fallback_is_color_bold
        else:
            fonts = self._skia_fallback_fonts
            color_flags = self._skia_fallback_is_color
        code = ord(ch[0])
        for font, is_color in zip(fonts, color_flags):
            if font.unicharToGlyph(code):
                return font, is_color
        # Fall back to the primary so the renderer at least draws
        # .notdef instead of silently skipping.
        return fonts[0], color_flags[0]

    def _draw_glyph(
        self,
        ch: str,
        bold: bool,
        x: int,
        y: int,
        fg: tuple[int, int, int],
        bg: tuple[int, int, int],
        cell_w: int,
    ) -> None:
        """Rasterize one glyph via Skia and paste into self.image.

        Skia gives us subpixel positioning, LCD filtering, and stem
        darkening that Pillow+FreeType doesn't expose. The tradeoff
        is one tiny offscreen surface per glyph — acceptable for
        dirty-row rendering at ~80 chars/row × 30 rows.
        """
        w = cell_w
        h = self.cell_height
        surface = skia.Surface.MakeRasterN32Premul(w, h)
        canvas = surface.getCanvas()
        canvas.clear(skia.ColorSetRGB(*bg))
        font, is_color = self._find_font_for_char(ch, bold=bold)
        paint = skia.Paint(AntiAlias=True)
        if not is_color:
            # Color-bitmap fonts (Apple Color Emoji) carry their own
            # sbix pixels. Applying Paint.setColor would tint the
            # bitmap to the fg color, destroying the emoji art.
            paint.setColor(skia.ColorSetRGB(*fg))
        canvas.drawString(ch, 0, self._skia_baseline, font, paint)
        # Snapshot -> RGBA bytes -> paste into self.image.
        # skia N32Premul is BGRA on little-endian Apple silicon; use
        # encodeToData(PNG) if byte-order matters, but for speed we
        # use peekPixels and swizzle.
        img_info = skia.ImageInfo.MakeN32Premul(w, h)
        buf = bytearray(w * h * 4)
        surface.readPixels(img_info, buf, w * 4, 0, 0)
        # Skia's N32 is RGBA_8888 on macOS arm64 (and RGBA by default
        # on most modern builds), so no byte swizzle needed.
        tile = Image.frombytes("RGBA", (w, h), bytes(buf))
        self.image.paste(tile, (x, y))

    def resize(self, cols: int, rows: int) -> None:
        """Resize the framebuffer to new terminal dimensions."""
        self.cols = cols
        self.rows = rows
        self.width = cols * self.cell_width + 2 * self.padding
        self.height = rows * self.cell_height + 2 * self.padding
        self.image = Image.new("RGBX", (self.width, self.height), DEFAULT_BG)
        self._prev_cursor = (-1, -1)

    def _resolve_color(
        self, color: str, bold: bool = False, is_bg: bool = False
    ) -> tuple[int, int, int]:
        """Map a pyte color value to an amber-tinted RGB tuple."""
        if color == "default" or color is None:
            if is_bg:
                return DEFAULT_BG
            return BOLD_FG if bold else DEFAULT_FG

        # Named color
        if color in AMBER_COLORS:
            if bold and not is_bg and not color.startswith("bright"):
                # "brown" promotes to "brightyellow" per standard ANSI
                bright_name = "brightyellow" if color == "brown" else ("bright" + color)
                if bright_name in AMBER_COLORS:
                    return AMBER_COLORS[bright_name]
            return AMBER_COLORS[color]

        # Integer index (256-color)
        if isinstance(color, int) or (isinstance(color, str) and color.isdigit()):
            idx = int(color)
            if 0 <= idx < 256:
                return _PALETTE_256[idx]

        # 6-char hex string (truecolor). We route it through the
        # current theme's DEFAULT_BG → DEFAULT_FG luminance ramp so a
        # theme switch actually recolors the output. The old amber-
        # hardcoded warm-shift was theme-agnostic and made Claude
        # Code's truecolor borders / banner stay amber even after
        # switching to dark/light.
        if isinstance(color, str) and len(color) == 6:
            try:
                r = int(color[0:2], 16)
                g = int(color[2:4], 16)
                b = int(color[4:6], 16)
                lum = (r * 299 + g * 587 + b * 114) / 1000 / 255.0
                bg_r, bg_g, bg_b = DEFAULT_BG
                fg_r, fg_g, fg_b = DEFAULT_FG
                return (
                    int(bg_r + (fg_r - bg_r) * lum),
                    int(bg_g + (fg_g - bg_g) * lum),
                    int(bg_b + (fg_b - bg_b) * lum),
                )
            except ValueError:
                pass

        return DEFAULT_FG

    def render_dirty(
        self,
        screen: pyte.Screen,
        dirty_rows: set[int],
        selection: tuple[tuple[int, int], tuple[int, int]] | None = None,
    ) -> list[tuple[int, int, int, int, "Image.Image"]]:
        """Re-render dirty rows. Returns list of (x, y, w, h, PIL.Image) rectangles.

        If `selection` is given as ``((start_col, start_row), (end_col, end_row))``
        in reading order, cells inside the range are drawn with inverted
        foreground/background colors to indicate highlight.
        """
        if not dirty_rows:
            return []

        draw = ImageDraw.Draw(self.image)
        rects = []

        pad = self.padding
        inner_w = self.cols * self.cell_width

        for row in sorted(dirty_rows):
            if row >= self.rows:
                continue
            y = row * self.cell_height + pad

            # Clear the cell-grid area for this row (leave side padding alone).
            draw.rectangle(
                [pad, y, pad + inner_w - 1, y + self.cell_height - 1],
                fill=DEFAULT_BG,
            )

            line = screen.buffer[row]
            skip_next = 0
            for col in range(self.cols):
                if skip_next:
                    skip_next -= 1
                    continue

                char = line[col]
                x = col * self.cell_width + pad
                ch = char.data

                fg = self._resolve_color(char.fg, char.bold, is_bg=False)
                bg = self._resolve_color(char.bg, False, is_bg=True)

                if char.reverse:
                    fg, bg = bg, fg

                # Selection highlight: invert after reverse handling so a
                # reverse-video cell inside a selection returns to normal.
                if _cell_in_selection(col, row, selection):
                    fg, bg = bg, fg

                # Determine cell span via wcwidth.
                #   2  -> double-wide (CJK, some emoji)
                #   1  -> normal
                #   0  -> combining mark (overlay onto previous cell)
                #  -1  -> control/unassigned (treat as 1)
                w = wcwidth.wcwidth(ch) if ch else 1
                cells = 2 if w == 2 else 1
                cell_px = self.cell_width * cells

                # Combining mark: overlay on previous cell. For now
                # we just draw it over the previous cell's bg — Skia
                # doesn't support stacking marks trivially. Acceptable
                # since combining marks are rare in terminal output.
                if w == 0 and col > 0 and ch:
                    prev_x = (col - 1) * self.cell_width + pad
                    self._draw_glyph(
                        ch, char.bold, prev_x, y, fg, DEFAULT_BG,
                        self.cell_width,
                    )
                    continue

                # Draw the cell: background + glyph in one Skia pass.
                # _draw_glyph fills bg first then draws the glyph, so
                # we don't need a separate rectangle clear.
                if ch and ch != " ":
                    self._draw_glyph(
                        ch, char.bold, x, y, fg, bg, cell_px,
                    )
                elif bg != DEFAULT_BG:
                    # Empty cell with a non-default background.
                    draw.rectangle(
                        [x, y, x + cell_px - 1, y + self.cell_height - 1],
                        fill=bg,
                    )

                # Underline — skip on whitespace and box-drawing cells.
                # Claude Code (and some other TUIs) set the underscore
                # attribute on whole rows of separator characters
                # (`─`, spaces). Rendering a full-width underline bar
                # under those produces a visible dark horizontal valley.
                # For real text (URLs, prompts) the underline is still
                # drawn in the cell's fg color.
                if char.underscore and ch and ch not in _NO_UNDERLINE:
                    ul_y = y + self.cell_height - 2
                    draw.line(
                        [x, ul_y, x + cell_px - 1, ul_y], fill=fg,
                    )

                if cells == 2:
                    skip_next = 1

            # Return a crop view of the row (inner area only). Skipping
            # the side padding saves a few bytes per row and keeps the
            # update bounded to cells that actually changed.
            row_img = self.image.crop(
                (pad, y, pad + inner_w, y + self.cell_height)
            )
            rects.append((pad, y, inner_w, self.cell_height, row_img))

        return rects

    def render_cursor(
        self, screen: pyte.Screen
    ) -> tuple[int, int, int, int, "Image.Image"] | None:
        """Render cursor block. Returns (x, y, w, h, PIL.Image) or None."""
        cx, cy = screen.cursor.x, screen.cursor.y
        if cx >= self.cols or cy >= self.rows:
            return None

        char = screen.buffer[cy][cx]
        ch = char.data
        w = wcwidth.wcwidth(ch) if ch else 1
        cells = 2 if w == 2 else 1
        cell_px = self.cell_width * cells

        x = cx * self.cell_width + self.padding
        y = cy * self.cell_height + self.padding

        # Draw cursor as a filled block with inverted colors.
        cursor_img = self.image.crop(
            (x, y, x + cell_px, y + self.cell_height)
        ).copy()
        draw = ImageDraw.Draw(cursor_img)
        draw.rectangle(
            [0, 0, cell_px - 1, self.cell_height - 1],
            fill=CURSOR_COLOR,
        )

        # Redraw character under cursor with inverted color.
        if ch and ch != " ":
            draw.text(
                (self._x_offset, self._y_offset),
                ch, font=self.font, fill=DEFAULT_BG,
            )

        self._prev_cursor = (cx, cy)
        return (x, y, cell_px, self.cell_height, cursor_img)

    def full_render(
        self,
        screen: pyte.Screen,
        selection: tuple[tuple[int, int], tuple[int, int]] | None = None,
    ) -> "Image.Image":
        """Render the entire screen. Returns the full PIL Image."""
        all_rows = set(range(self.rows))
        self.render_dirty(screen, all_rows, selection=selection)
        cursor = self.render_cursor(screen)
        if cursor:
            x, y, _, _, cursor_img = cursor
            self.image.paste(cursor_img, (x, y))
        return self.image
