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

from . import palette as _palette_mod
from . import theme as _theme_mod
from .theme import (
    AMBER_COLORS,
    THEMES,
    apply_theme,
)
from .palette import _STANDARD_PALETTE_256

log = logging.getLogger(__name__)


# Chars that should NEVER get an underline drawn under them, even if
# the cell has underscore=True. Covers ASCII space, NBSP, and the
# whole Unicode box-drawing block so separator rows don't produce a
# visible dark band.
_NO_UNDERLINE = frozenset(
    [" ", "\xa0"] + [chr(c) for c in range(0x2500, 0x2580)]
)

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
    # cell grid, like a CRT's overscan region. Mostly cosmetic -- it
    # keeps the first column of text from hugging the screen edge.
    PADDING = 5

    def __init__(
        self,
        cols: int = 80,
        rows: int = 24,
        font_path: str | None = None,
        font_size: int = 16,
        line_height: float = 1.0,
        contrast: str = "normal",
        color_mode: str = "phosphor",
    ):
        self.cols = cols
        self.rows = rows
        self.padding = self.PADDING
        self.contrast = contrast
        self.color_mode = color_mode
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
        # only -- Skia does the drawing so we get subpixel positioning,
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

        _emoji_candidates = [
            # macOS system font (Apple Color Emoji -- sbix color bitmaps)
            Path("/System/Library/Fonts/Apple Color Emoji.ttc"),
            # Vendored fallback for Linux CI and other non-macOS platforms
            # (Noto Color Emoji -- CBDT/CBLC color bitmaps, OFL 1.1)
            _VENDORED_FONT_DIR / "NotoColorEmoji.ttf",
        ]
        for _emoji_path in _emoji_candidates:
            if _emoji_path.is_file():
                tf = skia.Typeface.MakeFromFile(str(_emoji_path))
                if tf is not None:
                    emoji_font = skia.Font(tf, font_size)
                    self._skia_fallback_fonts.append(emoji_font)
                    self._skia_fallback_is_color.append(True)
                    # Bold reuses the same emoji font -- color emoji has no
                    # weight axis.
                    self._skia_fallback_fonts_bold.append(emoji_font)
                    self._skia_fallback_is_color_bold.append(True)
                    break

        # Apply shared rasterization flags to every fallback font.
        for f in (
            *self._skia_fallback_fonts,
            *self._skia_fallback_fonts_bold,
        ):
            f.setSubpixel(True)
            # Grayscale AA (not LCD subpixel) -- amber-on-black has no
            # blue channel transition, so subpixel rendering produces
            # red/green fringes that hurt perceived crispness. Plain
            # gray AA stays out of the way.
            f.setEdging(skia.Font.Edging.kAntiAlias)
            # In high-contrast mode, drop hinting to kNone so glyph
            # outlines are rasterized without pixel-snap; the resulting
            # strokes are thicker (1.5-2 px wide instead of hard-snapped
            # 1 px) and look noticeably brighter at small sizes.
            f.setHinting(
                skia.FontHinting.kNone if contrast == "high"
                else skia.FontHinting.kFull
            )
            # Let SF Mono's native TrueType hints drive -- they're
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
        # getlength() returns the horizontal advance -- the correct metric for
        # grid layout in a monospace font. Use math.ceil so we always
        # round UP; int(round()) can undersize the cell by ~0.5 px for
        # fonts whose advance is fractional (SF Mono at 13pt -> 7.8,
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
        self.image = Image.new("RGBX", (self.width, self.height), _theme_mod.DEFAULT_BG)
        self._prev_cursor = (-1, -1)

    def _find_font_for_char(
        self, ch: str, bold: bool,
    ) -> tuple["skia.Font", bool]:
        """Pick the first fallback font that has a glyph for ``ch``.

        Returns ``(font, is_color)`` where is_color signals that the
        font renders via color bitmaps (Apple Color Emoji) and the
        caller must not apply a Paint color -- that would tint the
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
        is one tiny offscreen surface per glyph -- acceptable for
        dirty-row rendering at ~80 chars/row x 30 rows.
        """
        w = cell_w
        h = self.cell_height
        surface = skia.Surface.MakeRasterN32Premul(w, h)
        canvas = surface.getCanvas()
        canvas.clear(skia.ColorSetRGB(*bg))
        font, is_color = self._find_font_for_char(ch, bold=bold)
        paint = skia.Paint(AntiAlias=True)
        if not is_color or self.color_mode == "phosphor":
            # Color-bitmap fonts (Apple Color Emoji / Noto Color Emoji)
            # carry their own sbix/CBDT pixels; applying Paint.setColor
            # tints the bitmap to the fg color, destroying the art. The
            # exception is phosphor mode, which collapses every hue to the
            # theme's single-hue ramp -- emoji must be tinted amber too so
            # no blue-dominant pixels leak through from the color bitmaps.
            paint.setColor(skia.ColorSetRGB(*fg))
        canvas.drawString(ch, 0, self._skia_baseline, font, paint)
        if not is_color:
            # Embolden via repeated draws at sub-pixel offsets.
            # Each extra draw thickens strokes without the jaggies of
            # 1-bit AA. Skipped for color bitmap emoji (would smear
            # the sbix bitmap).
            if self.contrast == "high":
                # +1 horizontal draw -> strokes are ~1.5-2 px wide
                canvas.drawString(ch, 1, self._skia_baseline, font, paint)
            elif self.contrast == "max":
                # Horizontal + diagonal half-step -> thicker than
                # "high" without the blocky smear of a 4-corner
                # draw. Keeps curve outlines smooth.
                canvas.drawString(ch, 1, self._skia_baseline, font, paint)
                canvas.drawString(
                    ch, 0.5, self._skia_baseline + 0.5, font, paint,
                )
        # Snapshot -> RGBA bytes -> paste into self.image.
        # Skia's native N32 colour type is BGRA on Linux (little-endian
        # x86) and RGBA on macOS arm64. To get a consistent channel order
        # we request kRGBA_8888_SkColorType explicitly; Skia performs the
        # BGRA->RGBA swizzle during readPixels if needed.
        img_info = skia.ImageInfo.Make(
            w, h,
            skia.kRGBA_8888_ColorType,
            skia.kPremul_AlphaType,
        )
        buf = bytearray(w * h * 4)
        surface.readPixels(img_info, buf, w * 4, 0, 0)
        tile = Image.frombytes("RGBA", (w, h), bytes(buf))
        self.image.paste(tile, (x, y))

    def resize(self, cols: int, rows: int) -> None:
        """Resize the framebuffer to new terminal dimensions."""
        self.cols = cols
        self.rows = rows
        self.width = cols * self.cell_width + 2 * self.padding
        self.height = rows * self.cell_height + 2 * self.padding
        self.image = Image.new("RGBX", (self.width, self.height), _theme_mod.DEFAULT_BG)
        self._prev_cursor = (-1, -1)

    def _resolve_color(
        self,
        color: str,
        bold: bool = False,
        is_bg: bool = False,
        cell_bg: tuple[int, int, int] | None = None,
    ) -> tuple[int, int, int]:
        """Map a pyte color value to an amber-tinted RGB tuple."""
        if color == "default" or color is None:
            if is_bg:
                return _theme_mod.DEFAULT_BG
            return _theme_mod.BOLD_FG if bold else _theme_mod.DEFAULT_FG

        # Named color
        if color in AMBER_COLORS:
            if bold and not is_bg and not color.startswith("bright"):
                # "brown" promotes to "brightyellow" per standard ANSI
                bright_name = "brightyellow" if color == "brown" else ("bright" + color)
                if bright_name in AMBER_COLORS:
                    return AMBER_COLORS[bright_name]
            return AMBER_COLORS[color]

        # Integer index (ANSI 0-255)
        if isinstance(color, int) or (
            isinstance(color, str) and len(color) <= 3 and color.isdigit()
        ):
            idx = int(color)
            if 0 <= idx < 256:
                if self.color_mode == "true-color":
                    return _STANDARD_PALETTE_256[idx]
                # phosphor (default): every SGR index collapses to a
                # brightness variant of the theme's single hue.
                return _palette_mod._PALETTE_PHOSPHOR[idx]

        # 6-char hex string (truecolor). fg is routed through the
        # theme's bg->fg OKLab ramp with a gamma-0.4 lift so dim greys
        # like Claude Code's #808080 hints stay WCAG-AA readable.
        # bg passes through raw in true-color mode (preserves TUI
        # backdrop intent); phosphor mode collapses it to the ramp.
        if isinstance(color, str) and len(color) == 6:
            try:
                r = int(color[0:2], 16)
                g = int(color[2:4], 16)
                b = int(color[4:6], 16)
                ramp_rgb = self._apply_oklab_ramp(
                    r, g, b, is_bg=is_bg, cell_bg=cell_bg,
                )
                if self.color_mode == "true-color":
                    return (r, g, b) if is_bg else ramp_rgb
                return ramp_rgb
            except ValueError:
                pass

        return _theme_mod.DEFAULT_FG

    def _apply_oklab_ramp(
        self,
        r: int,
        g: int,
        b: int,
        is_bg: bool,
        cell_bg: tuple[int, int, int] | None,
    ) -> tuple[int, int, int]:
        """Map a raw truecolor input onto the theme's bg->fg OKLab ramp.

        Returns the ramp-mapped RGB regardless of mode; callers decide
        whether to use it raw, blended, or as a full replacement.

        Preserves the orientation flip, cell_bg-aware reference frame,
        L_eff floor, and gamma 0.4 lift introduced earlier so Claude
        Code's mid-grey hint colors clear WCAG AA on every theme.
        """
        from .oklch import srgb_to_oklab
        L, _, _ = srgb_to_oklab(r, g, b)
        # Choose the lift's reference frame. If the caller told us the
        # cell's actual bg (via cell_bg), use that -- fg should always be
        # pushed *away* from the bg it will be painted against,
        # regardless of theme defaults. Falling back to DEFAULT_BG /
        # DEFAULT_FG when cell_bg is unknown preserves legacy behavior.
        if cell_bg is not None:
            bg_L, _, _ = srgb_to_oklab(*cell_bg)
            if bg_L >= 0.5:
                # Light cell bg: push fg toward black so the darkest
                # input intent (L=0) lands on black.
                ref_bg = (255, 255, 255)
                ref_fg = (0, 0, 0)
            else:
                ref_bg = (0, 0, 0)
                ref_fg = (255, 255, 255)
            ref_bg_L = 1.0 if bg_L >= 0.5 else 0.0
            ref_fg_L = 0.0 if bg_L >= 0.5 else 1.0
        else:
            ref_bg = _theme_mod.DEFAULT_BG
            ref_fg = _theme_mod.DEFAULT_FG
            ref_bg_L, _, _ = srgb_to_oklab(*_theme_mod.DEFAULT_BG)
            ref_fg_L, _, _ = srgb_to_oklab(*_theme_mod.DEFAULT_FG)
        # The lift parameterizes the input on a bg->fg perceptual ramp.
        # On dark-on-light orientations (ref_bg brighter than ref_fg),
        # L=0 is the user's darkest intent and should map to ref_fg
        # (black), not ref_bg (white). Flip the parameter so the ramp
        # always goes "user intent low -> bg, user intent high -> fg"
        # relative to the chosen orientation.
        L_eff = L if ref_fg_L >= ref_bg_L else (1.0 - L)
        # Floor L_eff so an input that lands on (or very near) the bg
        # pole still gets enough lift to escape it. Rescues the
        # "fg=#000000 on a dark cell bg" case where the user's literal
        # intent is invisible -- we honor the neutrality (still grey)
        # but force enough delta from the bg to clear ~9:1.
        L_eff = max(L_eff, 0.5)
        # Clamp L into [0, 1] then apply gamma lift. The 0.4 exponent
        # was tuned against Claude Code's actual grey inputs (#808080,
        # #949494, #d78787) to clear 4.5:1 on every built-in theme bg.
        t = max(0.0, min(1.0, L_eff)) ** 0.4
        bg_r, bg_g, bg_b = ref_bg
        fg_r, fg_g, fg_b = ref_fg
        return (
            int(bg_r + (fg_r - bg_r) * t),
            int(bg_g + (fg_g - bg_g) * t),
            int(bg_b + (fg_b - bg_b) * t),
        )

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
                fill=_theme_mod.DEFAULT_BG,
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

                bg = self._resolve_color(char.bg, False, is_bg=True)
                fg = self._resolve_color(
                    char.fg, char.bold, is_bg=False, cell_bg=bg,
                )

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
                # we just draw it over the previous cell's bg -- Skia
                # doesn't support stacking marks trivially. Acceptable
                # since combining marks are rare in terminal output.
                if w == 0 and col > 0 and ch:
                    prev_x = (col - 1) * self.cell_width + pad
                    self._draw_glyph(
                        ch, char.bold, prev_x, y, fg, _theme_mod.DEFAULT_BG,
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
                elif bg != _theme_mod.DEFAULT_BG:
                    # Empty cell with a non-default background.
                    draw.rectangle(
                        [x, y, x + cell_px - 1, y + self.cell_height - 1],
                        fill=bg,
                    )

                # Underline -- skip on whitespace and box-drawing cells.
                # Claude Code (and some other TUIs) set the underscore
                # attribute on whole rows of separator characters
                # (`-`, spaces). Rendering a full-width underline bar
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
            fill=_theme_mod.CURSOR_COLOR,
        )

        # Redraw character under cursor with inverted color.
        if ch and ch != " ":
            draw.text(
                (self._x_offset, self._y_offset),
                ch, font=self.font, fill=_theme_mod.DEFAULT_BG,
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
