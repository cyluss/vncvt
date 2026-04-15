#!/usr/bin/env python3
"""Measure actual rendered contrast in theme showcase screenshots.

Two metrics per theme:
- Modal: WCAG ratio between the two most common colors in the
  rendered screenshot (background vs body text). This is the
  contrast you actually perceive looking at the screen.
- Peak: theoretical max fg/bg contrast using the lightest and
  darkest pixel present. Matches the numbers you get from pure
  palette math (e.g. "light = 21.00:1 black on white").

Usage:
    uv run python scripts/measure_contrast.py
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from PIL import Image


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SHOTS = _REPO_ROOT / "docs" / "screenshots"
_THEMES = ["amber", "dark", "light", "green", "powershell"]


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _wcag_lum(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return (
        0.2126 * _srgb_to_linear(r / 255.0)
        + 0.7152 * _srgb_to_linear(g / 255.0)
        + 0.0722 * _srgb_to_linear(b / 255.0)
    )


def _wcag(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    l1, l2 = _wcag_lum(fg), _wcag_lum(bg)
    return (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)


def _measure(png_path: Path) -> tuple[
    tuple[int, int, int], tuple[int, int, int], float, float
]:
    """Return (bg_rgb, text_rgb, modal_contrast, peak_contrast)."""
    img = Image.open(png_path).convert("RGB")
    raw = img.tobytes()
    # Quantize to 16 levels per channel to find modal colors despite AA
    counter: Counter = Counter()
    for i in range(0, len(raw), 3):
        q = (raw[i] >> 4, raw[i + 1] >> 4, raw[i + 2] >> 4)
        counter[q] += 1

    def _unq(q: tuple[int, int, int]) -> tuple[int, int, int]:
        return (q[0] * 17, q[1] * 17, q[2] * 17)

    top = counter.most_common(10)
    bg_rgb = _unq(top[0][0])
    bg_l = _wcag_lum(bg_rgb)
    text_rgb: tuple[int, int, int] = _unq(top[1][0])
    for q, _count in top[1:]:
        candidate = _unq(q)
        if abs(_wcag_lum(candidate) - bg_l) > 0.1:
            text_rgb = candidate
            break

    # Peak: 0.1%/99.9% percentiles by luminance
    lums: list[tuple[float, tuple[int, int, int]]] = []
    for i in range(0, len(raw), 3):
        px = (raw[i], raw[i + 1], raw[i + 2])
        lums.append((_wcag_lum(px), px))
    lums.sort(key=lambda x: x[0])
    darkest = lums[max(0, int(len(lums) * 0.001))][1]
    lightest = lums[min(len(lums) - 1, int(len(lums) * 0.999))][1]

    return bg_rgb, text_rgb, _wcag(text_rgb, bg_rgb), _wcag(lightest, darkest)


def main() -> int:
    print(f"{'theme':11}  {'bg':15}  {'text (2nd mode)':17}  {'modal':>8}  {'peak':>8}")
    print("-" * 72)
    for theme in _THEMES:
        path = _SHOTS / f"claude-code-init-{theme}.png"
        if not path.is_file():
            print(f"{theme:11}  (missing {path})")
            continue
        bg, text, modal, peak = _measure(path)
        print(
            f"{theme:11}  {str(bg):15}  {str(text):17}  "
            f"{modal:>6.2f}:1  {peak:>6.2f}:1"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
