#!/usr/bin/env python3
"""Regenerate the theme showcase screenshots in docs/screenshots/.

Spawns vncvt for each built-in theme, runs Claude Code inside a
temporary project directory, captures the welcome panel, writes
it to ``docs/screenshots/claude-code-init-<theme>.png``.

Usage:
    uv run python scripts/refresh_showcase.py

Claude Code's own theme is set via a per-theme --settings override
so the rendered UI uses dark-ansi / light-ansi that matches the
vncvt theme (otherwise e.g. amber vncvt shows claude in its default
light theme and looks weird).

The script is read by humans; it's not in the test suite because
it depends on the local claude binary + network access to Claude
Code's welcome-screen renderer.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import asyncvnc
from PIL import Image

# Make sure we can import from the repo root when run as a script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from tests.scenes import trigger_server_dump  # noqa: E402
from vncvt.supervisor import start_vncvt, stop_vncvt  # noqa: E402


# Top content-area height in pixels. Claude Code's welcome panel
# fits in the first 7-8 rows; 180 px covers all of it at 13pt + 1.1
# line height and crops out the empty bottom 2/3 of the framebuffer.
_CROP_HEIGHT = 180


CLAUDE_BIN = Path.home() / ".local" / "bin" / "claude"
OUTPUT_DIR = _REPO_ROOT / "docs" / "screenshots"

# Map vncvt theme name → Claude Code's own theme key. The "-ansi"
# suffix tells Claude Code to emit plain SGR ANSI codes (not truecolor
# hex) so vncvt's per-theme ANSI palette drives the final colors.
VNCVT_TO_CLAUDE_THEME = {
    "amber":  "dark-ansi",
    "dark":   "dark-ansi",
    "green":  "dark-ansi",
    "light":  "light-ansi",
    "c64":    "dark-ansi",
    "dos":    "dark-ansi",
    "atari":  "dark-ansi",
}


async def capture_theme(vncvt_theme: str) -> Path:
    """Spawn vncvt with Claude Code + the given theme; save screenshot."""
    claude_theme = VNCVT_TO_CLAUDE_THEME[vncvt_theme]

    with TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        settings_path = tmpdir / "cc-settings.json"
        settings_path.write_text(json.dumps({"theme": claude_theme}))

        # Wrapper script so the shell that vncvt spawns is really
        # claude with the right --settings.
        wrapper = tmpdir / "cc-wrapper.sh"
        wrapper.write_text(
            f"#!/bin/bash\n"
            f"exec {CLAUDE_BIN} --settings {settings_path} \"$@\"\n"
        )
        wrapper.chmod(0o755)

        handle = start_vncvt(
            "--shell", str(wrapper),
            "--theme", vncvt_theme,
            "--font-size", "13",
            "--line-height", "1.1",
            scene_root=tmpdir / "scenes",
        )
        try:
            async with asyncvnc.connect(
                host=handle.host, port=handle.port,
            ) as vnc:
                # Wait for trust dialog to paint
                await asyncio.sleep(3.0)
                # Accept trust prompt (default is "Yes, I trust")
                vnc.keyboard.press("Return")
                # Wait for welcome panel to paint
                await asyncio.sleep(4.0)
                # Pull a few screenshots to force asyncvnc's
                # internal buffer to refresh to the latest frame.
                for _ in range(3):
                    await vnc.screenshot()
                    await asyncio.sleep(0.3)

                scene_dir = await trigger_server_dump(
                    handle.control_socket, f"showcase-{vncvt_theme}",
                )
                # Keep the full-height screenshot for archival / debug.
                full_path = OUTPUT_DIR / f"claude-code-init-{vncvt_theme}.png"
                shutil.copy(scene_dir / "scene.fb.png", full_path)
                # Crop to the content area and write claude-code-<theme>.png
                # — this is what the README references so block <img> tags
                # render at native size without wasted whitespace.
                cropped_path = OUTPUT_DIR / f"claude-code-{vncvt_theme}.png"
                img = Image.open(full_path).convert("RGB")
                cropped = img.crop((0, 0, img.width, _CROP_HEIGHT))
                cropped.save(cropped_path)
                print(
                    f"  {vncvt_theme:11} -> "
                    f"{cropped_path.relative_to(_REPO_ROOT)}"
                )
                return cropped_path
        finally:
            stop_vncvt(handle)


async def main() -> int:
    if not CLAUDE_BIN.is_file():
        print(f"ERROR: claude binary not found at {CLAUDE_BIN}", file=sys.stderr)
        return 1
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Capturing theme showcase to {OUTPUT_DIR.relative_to(_REPO_ROOT)}/")
    # Don't parallelize — each capture binds to port 5900 (or random).
    for theme in VNCVT_TO_CLAUDE_THEME:
        await capture_theme(theme)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
