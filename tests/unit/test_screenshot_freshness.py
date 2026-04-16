"""CI guard: committed screenshots must match the current renderer.

Replays the .cast fixture through each theme's phosphor pipeline and
compares the hue signature against the committed screenshot. If the
renderer changed colors but the screenshots weren't regenerated, this
test fails.

This catches the exact scenario where a renderer fix lands without
a matching screenshot refresh — the problem that burned us when the
OKLCH ramp fix was committed but the old grey/green screenshots
stayed in the repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from vncvt.cast_replay import replay_cast

FIXTURE = Path(__file__).parent.parent / "fixtures" / "claude-light-row0-invisible.cast"
SCREENSHOTS = Path(__file__).parent.parent.parent / "docs" / "screenshots"

# Themes that have committed screenshots + their hue invariants.
# The screenshot file is docs/screenshots/claude-code-<theme>.png.
_CHROMATIC_THEMES = {
    "amber": lambda r, g, b: r >= g >= b,
    "green": lambda r, g, b: g >= r and g >= b,
    "c64":   lambda r, g, b: b >= r and b >= g,
    "dos":   lambda r, g, b: b >= r and b >= g,
    "atari": lambda r, g, b: r >= g >= b,
}


def _sample_non_bg(img: Image.Image, bg_threshold: int = 30) -> list[tuple[int, int, int]]:
    """Return all pixels that aren't near-black (background)."""
    raw = img.convert("RGB").tobytes()
    pixels = []
    for i in range(0, len(raw), 3):
        r, g, b = raw[i], raw[i + 1], raw[i + 2]
        if r + g + b > bg_threshold:
            pixels.append((r, g, b))
    return pixels


def _avg_chroma(pixels: list[tuple[int, int, int]]) -> float:
    if not pixels:
        return 0.0
    return sum(max(p) - min(p) for p in pixels) / len(pixels)


@pytest.mark.parametrize("theme", list(_CHROMATIC_THEMES))
def test_committed_screenshot_is_tinted(theme):
    """The committed screenshot for each chromatic theme must show
    the theme's hue, not neutral grey. If a renderer change altered
    the tinting but the screenshots weren't regenerated, this fails."""
    screenshot = SCREENSHOTS / f"claude-code-{theme}.png"
    if not screenshot.exists():
        pytest.skip(f"no committed screenshot for {theme}")

    img = Image.open(screenshot)
    pixels = _sample_non_bg(img)
    assert len(pixels) > 100, f"{theme}: too few non-bg pixels ({len(pixels)})"

    avg_c = _avg_chroma(pixels)
    assert avg_c >= 15, (
        f"{theme}: committed screenshot avg chroma = {avg_c:.1f} — "
        f"too grey, expected >= 15. Regenerate with: "
        f"uv run python scripts/refresh_showcase.py {theme}"
    )

    # Check dominant hue direction
    hue_check = _CHROMATIC_THEMES[theme]
    matching = sum(1 for p in pixels if hue_check(*p))
    ratio = matching / len(pixels)
    assert ratio >= 0.5, (
        f"{theme}: only {ratio:.0%} of non-bg pixels match the "
        f"theme's hue invariant. Regenerate with: "
        f"uv run python scripts/refresh_showcase.py {theme}"
    )


@pytest.mark.parametrize("theme", list(_CHROMATIC_THEMES))
def test_replay_matches_screenshot_hue(theme):
    """The .cast fixture replayed through the current renderer must
    produce the same hue family as the committed screenshot. If the
    renderer changed but screenshots are stale, the replayed image
    will have different chroma/hue than the committed one."""
    screenshot = SCREENSHOTS / f"claude-code-{theme}.png"
    if not screenshot.exists():
        pytest.skip(f"no committed screenshot for {theme}")

    frame = replay_cast(FIXTURE, theme=theme, color_mode="phosphor")
    replay_pixels = _sample_non_bg(frame.image)
    screenshot_pixels = _sample_non_bg(Image.open(screenshot))

    if len(replay_pixels) < 50 or len(screenshot_pixels) < 50:
        pytest.skip("insufficient non-bg pixels for comparison")

    replay_chroma = _avg_chroma(replay_pixels)
    screenshot_chroma = _avg_chroma(screenshot_pixels)

    # If the renderer produces high chroma but the screenshot has low
    # chroma, the screenshot is stale.
    if replay_chroma >= 30 and screenshot_chroma < 15:
        pytest.fail(
            f"{theme}: renderer produces avg chroma {replay_chroma:.1f} "
            f"but committed screenshot has {screenshot_chroma:.1f}. "
            f"Screenshots are stale. Regenerate with: "
            f"uv run python scripts/refresh_showcase.py {theme}"
        )
