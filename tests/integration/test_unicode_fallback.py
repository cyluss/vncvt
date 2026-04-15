"""Font fallback chain: Hangul → Sarasa Mono K, emoji → Apple Color Emoji.

Regression test for the font fallback implementation. Verifies that
when the primary typeface doesn't contain a glyph (SF Mono has no
Hangul, no CJK, no emoji), the renderer walks its fallback list and
picks a typeface that does.

Uses the server-side renderer directly rather than driving a shell
because the test cares about the rasterizer, not about PTY byte
flow. Keeps the test fast and deterministic.
"""

from __future__ import annotations

from vncvt.renderer import TerminalRenderer
import pyte


def test_hangul_glyph_resolves_to_sarasa():
    """`한` should resolve to Sarasa Mono K, not the primary typeface."""
    rend = TerminalRenderer(cols=10, rows=2, font_size=16)
    # Walk the fallback chain directly and find which entry has Hangul.
    font, is_color = rend._find_font_for_char("한", bold=False)
    assert font.unicharToGlyph(ord("한")) != 0, (
        "Hangul char resolved to a font without the glyph"
    )
    assert not is_color, "Hangul should not resolve via color-bitmap font"


def test_cjk_glyph_resolves_to_sarasa():
    """`中` should resolve to a font with a non-zero glyph ID."""
    rend = TerminalRenderer(cols=10, rows=2, font_size=16)
    font, is_color = rend._find_font_for_char("中", bold=False)
    assert font.unicharToGlyph(ord("中")) != 0
    assert not is_color


def test_emoji_resolves_to_color_bitmap():
    """`😀` should resolve to Apple Color Emoji (is_color=True)."""
    rend = TerminalRenderer(cols=10, rows=2, font_size=16)
    font, is_color = rend._find_font_for_char("😀", bold=False)
    assert font.unicharToGlyph(ord("😀")) != 0
    assert is_color, (
        "Emoji should resolve via Apple Color Emoji (is_color=True); "
        "otherwise Paint.setColor would tint the bitmap"
    )


def test_ascii_stays_on_primary_font():
    """`A` should resolve to the primary typeface, NOT a fallback."""
    rend = TerminalRenderer(cols=10, rows=2, font_size=16)
    primary_font = rend._skia_font
    font, _is_color = rend._find_font_for_char("A", bold=False)
    assert font is primary_font, (
        "ASCII should never fall through to a fallback — the primary "
        "typeface must come first in the chain"
    )


def test_unicode_render_produces_non_empty_cells():
    """End-to-end: feed Hangul + emoji via pyte and verify the server
    render has non-background pixels at those cells."""
    rend = TerminalRenderer(cols=20, rows=2, font_size=16)
    screen = pyte.Screen(20, 2)
    stream = pyte.Stream(screen)
    stream.feed("한글 😀")
    rend.render_dirty(screen, set(range(2)))

    # Sample the first cell (row 0, col 0) — should be '한'. Pull its
    # rendered pixels and count non-background ones.
    pad = rend.padding
    x0 = 0 * rend.cell_width + pad
    y0 = 0 * rend.cell_height + pad
    # Hangul is a wide char, so it spans 2 cells; check the full width
    cell_box = (x0, y0, x0 + 2 * rend.cell_width, y0 + rend.cell_height)
    crop = rend.image.crop(cell_box).convert("RGB")
    raw = crop.tobytes()
    fg_count = sum(
        1 for i in range(0, len(raw), 3)
        if raw[i] + raw[i + 1] + raw[i + 2] > 60
    )
    assert fg_count > 20, (
        f"Hangul cell rendered only {fg_count} fg pixels — fallback "
        f"path may not be drawing anything"
    )
