#!/usr/bin/env python3
"""Regenerate the theme showcase screenshots in docs/screenshots/.

Reads ``docs/showcase.toml`` for the list of entries and per-entry
overrides (crop height, wait times, Claude Code theme, etc.), spawns
vncvt once per entry, drives Claude Code's welcome panel, captures
the framebuffer.

Usage:
    uv run python scripts/refresh_showcase.py          # all entries
    uv run python scripts/refresh_showcase.py amber    # just amber
    uv run python scripts/refresh_showcase.py c64 dos  # just these two
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory

import asyncvnc
from PIL import Image

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from tests.scenes import trigger_server_dump  # noqa: E402
from vncvt.supervisor import start_vncvt, stop_vncvt  # noqa: E402

SHOWCASE_TOML = _REPO_ROOT / "docs" / "showcase.toml"
CLAUDE_BIN = Path.home() / ".local" / "bin" / "claude"
OUTPUT_DIR = _REPO_ROOT / "docs" / "screenshots"

# Defaults used when showcase.toml omits a key.
_BUILTIN_DEFAULTS = {
    "claude-theme": "dark-ansi",
    "crop-height": 300,
    "font-size": 13,
    "line-height": 1.1,
    "wait-trust": 3.0,
    "wait-welcome": 6.0,
}


def _load_config() -> tuple[dict, list[dict]]:
    """Return (defaults, entries) from showcase.toml."""
    with SHOWCASE_TOML.open("rb") as f:
        raw = tomllib.load(f)
    defaults = {**_BUILTIN_DEFAULTS, **raw.get("defaults", {})}
    entries = raw.get("entry", [])
    if not entries:
        sys.exit(f"ERROR: no [[entry]] sections in {SHOWCASE_TOML}")
    return defaults, entries


def _get(entry: dict, defaults: dict, key: str):
    """Look up key in entry, fall back to defaults."""
    return entry.get(key, defaults[key])


async def capture_entry(entry: dict, defaults: dict) -> Path:
    """Spawn vncvt + Claude Code for one showcase entry."""
    theme = entry["theme"]
    claude_theme = _get(entry, defaults, "claude-theme")
    crop_height = int(_get(entry, defaults, "crop-height"))
    font_size = int(_get(entry, defaults, "font-size"))
    line_height = float(_get(entry, defaults, "line-height"))
    wait_trust = float(_get(entry, defaults, "wait-trust"))
    wait_welcome = float(_get(entry, defaults, "wait-welcome"))

    with TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        settings_path = tmpdir / "cc-settings.json"
        settings_path.write_text(json.dumps({"theme": claude_theme}))

        wrapper = tmpdir / "cc-wrapper.sh"
        wrapper.write_text(
            f"#!/bin/bash\n"
            f"exec {CLAUDE_BIN} --settings {settings_path} \"$@\"\n"
        )
        wrapper.chmod(0o755)

        handle = start_vncvt(
            "--shell", str(wrapper),
            "--theme", theme,
            "--font-size", str(font_size),
            "--line-height", str(line_height),
            scene_root=tmpdir / "scenes",
        )
        try:
            async with asyncvnc.connect(
                host=handle.host, port=handle.port,
            ) as vnc:
                await asyncio.sleep(wait_trust)
                vnc.keyboard.press("Return")
                await asyncio.sleep(wait_welcome)
                for _ in range(3):
                    await vnc.screenshot()
                    await asyncio.sleep(0.3)

                scene_dir = await trigger_server_dump(
                    handle.control_socket, f"showcase-{theme}",
                )
                full_path = OUTPUT_DIR / f"claude-code-init-{theme}.png"
                shutil.copy(scene_dir / "scene.fb.png", full_path)

                cropped_path = OUTPUT_DIR / f"claude-code-{theme}.png"
                img = Image.open(full_path).convert("RGB")
                cropped = img.crop((0, 0, img.width, crop_height))
                cropped.save(cropped_path)
                print(
                    f"  {theme:11} -> "
                    f"{cropped_path.relative_to(_REPO_ROOT)}"
                )
                return cropped_path
        finally:
            stop_vncvt(handle)


async def main() -> int:
    if not CLAUDE_BIN.is_file():
        print(f"ERROR: claude binary not found at {CLAUDE_BIN}", file=sys.stderr)
        return 1
    if not SHOWCASE_TOML.is_file():
        print(f"ERROR: {SHOWCASE_TOML} not found", file=sys.stderr)
        return 1

    defaults, entries = _load_config()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Filter to specific themes if given on the command line.
    if len(sys.argv) > 1:
        requested = set(sys.argv[1:])
        entries = [e for e in entries if e["theme"] in requested]
        missing = requested - {e["theme"] for e in entries}
        if missing:
            print(f"WARNING: no [[entry]] for: {sorted(missing)}", file=sys.stderr)
        if not entries:
            sys.exit("ERROR: no matching entries")

    print(f"Capturing {len(entries)} theme(s) to {OUTPUT_DIR.relative_to(_REPO_ROOT)}/")
    for entry in entries:
        await capture_entry(entry, defaults)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
