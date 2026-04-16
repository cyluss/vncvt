#!/usr/bin/env python3
"""Render a fake Claude Code conversation directly into vncvt's
TerminalRenderer, bypassing the real claude binary and the
Anthropic API. Useful for reproducing the "dim previous prompts
look invisible" bug without waiting on a round trip.

Usage:
    uv run python scripts/mock_claude_session.py \
        --theme light --out /tmp/mock-light.png

The script feeds a sequence of SGR escape codes that mimic what
Claude Code actually emits for:
  - The Claude Code welcome panel header
  - A previous user prompt (SGR 2 / faint, or a specific grey
    truecolor like #606060)
  - A previous assistant response (grey #808080)
  - The current prompt box (full fg)

Tweak MOCK_SCRIPT below to match the exact bytes Claude Code
sends if you have a real session capture. For the dim-prompt
investigation we just need SOMETHING with dim-grey fg text so we
can verify the renderer maps it to a readable pixel value.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from vncvt.renderer import TerminalRenderer, apply_theme  # noqa: E402
from vncvt.terminal import Terminal  # noqa: E402
from PIL import Image  # noqa: E402


# Grey candidates that might represent Claude Code's "dim previous
# prompt" rendering. Each row of the mock uses a different one so
# we can see which mapping produces visible vs invisible output on
# each theme.
_DIM_GREY_CANDIDATES = [
    ("SGR 2 faint",      "\x1b[2m"),
    ("ANSI brightblack", "\x1b[38;5;8m"),
    ("256 #808080",      "\x1b[38;5;244m"),
    ("truecolor 606060", "\x1b[38;2;96;96;96m"),
    ("truecolor 808080", "\x1b[38;2;128;128;128m"),
    ("truecolor 949494", "\x1b[38;2;148;148;148m"),
    ("truecolor 303030", "\x1b[38;2;48;48;48m"),
    ("truecolor 404040", "\x1b[38;2;64;64;64m"),
    ("truecolor d0d0d0", "\x1b[38;2;208;208;208m"),
]


def _build_mock() -> str:
    lines = []
    # Header: full-bright
    lines.append("\x1b[1m─── Mock Claude Code session ──────────\x1b[0m")
    lines.append("")
    for label, escape in _DIM_GREY_CANDIDATES:
        lines.append(f"{escape}{label:20} → previous response text goes here\x1b[0m")
    lines.append("")
    lines.append("\x1b[1m❯ \x1b[0mCurrent prompt (full brightness)")
    return "\r\n".join(lines) + "\r\n"


MOCK_SCRIPT = _build_mock()


def render_mock(theme: str, out_path: Path) -> None:
    apply_theme(theme)
    rend = TerminalRenderer(cols=80, rows=24, font_size=13, line_height=1.1)
    # Use a Terminal just for its pyte Screen+Stream — don't spawn a real shell
    import pyte
    screen = pyte.Screen(80, 24)
    stream = pyte.Stream(screen)
    stream.feed(MOCK_SCRIPT)
    img = rend.full_render(screen)
    img.convert("RGB").save(out_path)
    print(f"saved {out_path}")

    # Print the cell colors for the dim lines so we can see what
    # vncvt's _resolve_color produced.
    print()
    print("Cell-level fg inspection (rows 2-6):")
    for row in range(2, 7):
        cells = screen.buffer[row]
        chars_with_color = []
        for col in range(80):
            c = cells[col]
            if c.data and c.data != " ":
                chars_with_color.append(
                    (col, c.data, c.fg, c.bold, c.bg)
                )
        if chars_with_color:
            sample = chars_with_color[0]
            print(f"  row {row}: first char={sample[1]!r} "
                  f"fg={sample[2]} bold={sample[3]} bg={sample[4]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--theme", default="light",
        choices=("amber", "dark", "light", "green", "c64", "dos", "atari"),
    )
    parser.add_argument(
        "--out", type=Path, default=Path("/tmp/mock-claude.png"),
    )
    args = parser.parse_args()
    render_mock(args.theme, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
