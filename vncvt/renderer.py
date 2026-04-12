"""Terminal-to-pixel rendering with VT220 amber-tinted color scheme."""

import logging
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
import pyte
import wcwidth

log = logging.getLogger(__name__)


# Amber-tinted ANSI color palette
# Each standard ANSI color is warm-shifted toward amber phosphor tones
AMBER_COLORS = {
    "black":         (61, 40, 0),
    "red":           (255, 92, 0),
    "green":         (204, 163, 0),
    "brown":         (204, 140, 0),
    "blue":          (204, 128, 0),
    "magenta":       (255, 122, 61),
    "cyan":          (230, 184, 0),
    "white":         (255, 204, 102),
    "brightblack":   (102, 72, 10),
    "brightred":     (255, 140, 50),
    "brightgreen":   (255, 210, 50),
    "brightyellow":  (255, 230, 80),
    "brightblue":    (230, 170, 50),
    "brightmagenta": (255, 170, 100),
    "brightcyan":    (255, 220, 80),
    "brightwhite":   (255, 240, 170),
}

# pyte uses these names for the 8 basic colors
_PYTE_COLOR_NAMES = [
    "black", "red", "green", "brown", "blue", "magenta", "cyan", "white",
]

DEFAULT_BG = (26, 16, 0)       # #1a1000 — dark amber CRT off-black
DEFAULT_FG = (255, 156, 0)     # #ff9c00 — warm amber phosphor
BOLD_FG = (255, 200, 0)        # #ffc800 — brighter amber for bold
CURSOR_COLOR = (255, 156, 0)   # same as default FG

FONT_SEARCH_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf",
]

BOLD_FONT_SEARCH_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeMonoBold.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf",
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

    def __init__(
        self,
        cols: int = 80,
        rows: int = 24,
        font_path: str | None = None,
        font_size: int = 16,
    ):
        self.cols = cols
        self.rows = rows

        # Load fonts
        if font_path is None:
            font_path = _find_font(FONT_SEARCH_PATHS)
        if font_path is None:
            raise RuntimeError(
                "No monospace font found. Install dejavu-sans-mono or use --font."
            )
        self.font = ImageFont.truetype(font_path, font_size)

        bold_path = _find_font(BOLD_FONT_SEARCH_PATHS)
        self.font_bold = (
            ImageFont.truetype(bold_path, font_size) if bold_path else self.font
        )

        # Measure character cell using advance width, not ink bbox.
        # getlength() returns the horizontal advance — the correct metric for
        # grid layout in a monospace font.
        self.cell_width = int(round(self.font.getlength("M")))
        ascent, descent = self.font.getmetrics()
        self.cell_height = ascent + descent
        self._x_offset = 0
        self._y_offset = 0

        # Warn if bold font has a different advance (would cause grid drift).
        if self.font_bold is not self.font:
            bold_adv = int(round(self.font_bold.getlength("M")))
            if bold_adv != self.cell_width:
                log.warning(
                    "Bold font advance (%d) != regular (%d); bold glyphs "
                    "will be clipped to the regular cell width",
                    bold_adv, self.cell_width,
                )

        self.width = cols * self.cell_width
        self.height = rows * self.cell_height

        # Create framebuffer image
        self.image = Image.new("RGBX", (self.width, self.height), DEFAULT_BG)
        self._prev_cursor = (-1, -1)

    def resize(self, cols: int, rows: int) -> None:
        """Resize the framebuffer to new terminal dimensions."""
        self.cols = cols
        self.rows = rows
        self.width = cols * self.cell_width
        self.height = rows * self.cell_height
        self.image = Image.new(
            "RGBA", (self.width, self.height), DEFAULT_BG + (255,)
        )
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

        # 6-char hex string (truecolor) — amber tint it
        if isinstance(color, str) and len(color) == 6:
            try:
                r = int(color[0:2], 16)
                g = int(color[2:4], 16)
                b = int(color[4:6], 16)
                # Warm-shift toward amber
                lum = (r * 299 + g * 587 + b * 114) / 1000
                ar = int(r * 0.4 + lum * 0.6 * 255 / 255)
                ag = int(g * 0.2 + lum * 0.5 * 156 / 255)
                ab = int(b * 0.1 + lum * 0.1 * 50 / 255)
                return (min(ar, 255), min(ag, 255), min(ab, 255))
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

        for row in sorted(dirty_rows):
            if row >= self.rows:
                continue
            y = row * self.cell_height

            # Clear the entire row background
            draw.rectangle(
                [0, y, self.width, y + self.cell_height - 1],
                fill=DEFAULT_BG,
            )

            line = screen.buffer[row]
            skip_next = 0
            for col in range(self.cols):
                if skip_next:
                    skip_next -= 1
                    continue

                char = line[col]
                x = col * self.cell_width
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

                # Combining mark: overlay on previous cell without touching bg.
                if w == 0 and col > 0 and ch:
                    font = self.font_bold if char.bold else self.font
                    prev_x = (col - 1) * self.cell_width
                    draw.text(
                        (prev_x + self._x_offset, y + self._y_offset),
                        ch, font=font, fill=fg,
                    )
                    continue

                # Draw cell background if not default.
                if bg != DEFAULT_BG:
                    draw.rectangle(
                        [x, y, x + cell_px - 1, y + self.cell_height - 1],
                        fill=bg,
                    )

                # Draw character with overflow clipping.
                if ch and ch != " ":
                    font = self.font_bold if char.bold else self.font
                    glyph_adv = int(round(font.getlength(ch)))
                    if glyph_adv > cell_px:
                        # Glyph wider than its cell: render to a temp RGBA
                        # image and paste cropped to the cell bounds.
                        tmp = Image.new(
                            "RGBA",
                            (glyph_adv + 4, self.cell_height),
                            (0, 0, 0, 0),
                        )
                        tdraw = ImageDraw.Draw(tmp)
                        tdraw.text(
                            (self._x_offset, self._y_offset),
                            ch, font=font, fill=fg + (255,),
                        )
                        cropped = tmp.crop((0, 0, cell_px, self.cell_height))
                        self.image.paste(cropped, (x, y), cropped)
                    else:
                        draw.text(
                            (x + self._x_offset, y + self._y_offset),
                            ch, font=font, fill=fg,
                        )

                # Underline
                if char.underscore:
                    ul_y = y + self.cell_height - 2
                    draw.line([x, ul_y, x + cell_px - 1, ul_y], fill=fg)

                if cells == 2:
                    skip_next = 1

            # Return a crop view of the row — cheap, no copy until tobytes.
            row_img = self.image.crop((0, y, self.width, y + self.cell_height))
            rects.append((0, y, self.width, self.cell_height, row_img))

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

        x = cx * self.cell_width
        y = cy * self.cell_height

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
