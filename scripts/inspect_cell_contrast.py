#!/usr/bin/env python3
"""Per-cell contrast inspector for vncvt scene dumps.

Given a scene bundle (scene.fb.png + scene.term.json), walks every
non-empty cell and measures the actual rendered contrast between
the glyph's darkest fg pixel and the cell's background. Prints cells
that fall below a user-specified threshold — these are the ones
that look "invisible" or "dim" to the eye.

Usage:
    uv run python scripts/inspect_cell_contrast.py <scene_dir>
    uv run python scripts/inspect_cell_contrast.py <scene_dir> --min 4.5

Or live against a running vncvt + Claude Code:
    uv run python scripts/inspect_cell_contrast.py --live

The --live mode spawns vncvt with claude as the shell, waits for
the welcome panel, triggers a server dump, and inspects it all in
one step.

This script is a thin shim around ``vncvt.cast_replay.inspect_pixels``
so the WCAG math lives in exactly one place.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from vncvt.cast_replay import ContrastReport, inspect_pixels  # noqa: E402


def _inspect_bundle(scene_dir: Path, threshold: float) -> ContrastReport:
    """Load a scene bundle and run the shared WCAG kernel against it."""
    fb_path = scene_dir / "scene.fb.png"
    term_path = scene_dir / "scene.term.json"
    if not fb_path.is_file() or not term_path.is_file():
        raise FileNotFoundError(
            f"missing scene.fb.png or scene.term.json in {scene_dir}"
        )

    img = Image.open(fb_path).convert("RGB")
    term = json.loads(term_path.read_text())
    dims = term["dimensions"]
    cols = int(dims["cols"])
    rows = int(dims["rows"])
    raw_cells = term.get("cells", {})

    cells: dict[tuple[int, int], str] = {}
    for key, cell in raw_cells.items():
        col_s, row_s = key.split(",")
        cells[(int(col_s), int(row_s))] = cell.get("c", "")

    return inspect_pixels(img, cells, cols, rows, threshold=threshold)


async def _live_capture(threshold: float, theme: str) -> int:
    """Spawn vncvt + claude, dump, inspect."""
    import asyncvnc
    from vncvt.supervisor import start_vncvt, stop_vncvt
    from tests.scenes import trigger_server_dump

    claude = Path.home() / ".local" / "bin" / "claude"
    if not claude.is_file():
        print(f"ERROR: claude binary not found at {claude}", file=sys.stderr)
        return 1

    with TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        # Per-theme settings to match Claude Code's theme to ours
        cc_theme = "light-ansi" if theme == "light" else "dark-ansi"
        settings = tmpdir / "cc.json"
        settings.write_text(json.dumps({"theme": cc_theme}))
        wrapper = tmpdir / "cc.sh"
        wrapper.write_text(
            f"#!/bin/bash\nexec {claude} --settings {settings} \"$@\"\n"
        )
        wrapper.chmod(0o755)

        handle = start_vncvt(
            "--shell", str(wrapper),
            "--theme", theme,
            scene_root=tmpdir / "scenes",
        )
        try:
            async with asyncvnc.connect(
                host=handle.host, port=handle.port,
            ) as vnc:
                await asyncio.sleep(3.0)
                vnc.keyboard.press("Return")  # accept trust prompt
                await asyncio.sleep(4.0)
                for _ in range(3):
                    await vnc.screenshot()
                    await asyncio.sleep(0.3)
                scene_dir = await trigger_server_dump(
                    handle.control_socket, "inspect",
                )
                # The scene_dir is under handle.scene_dir which gets
                # cleaned up by stop_vncvt. Copy the two files we need
                # to a safe place inside tmp before teardown.
                keep = tmpdir / "keep"
                keep.mkdir()
                shutil.copy(scene_dir / "scene.fb.png", keep / "scene.fb.png")
                shutil.copy(scene_dir / "scene.term.json", keep / "scene.term.json")
        finally:
            stop_vncvt(handle)

        report = _inspect_bundle(keep, threshold)
        print(report.format())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "scene_dir", nargs="?", type=Path,
        help="Path to a scene dump directory (omit for --live).",
    )
    parser.add_argument(
        "--min", type=float, default=4.5,
        help="Minimum WCAG contrast. Cells below this are reported "
             "(default: 4.5, WCAG AA).",
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Spawn vncvt + claude, capture fresh, inspect.",
    )
    parser.add_argument(
        "--theme", default="light",
        help="Theme to use with --live (default: light).",
    )
    args = parser.parse_args()

    if args.live:
        return asyncio.run(_live_capture(args.min, args.theme))

    if args.scene_dir is None:
        parser.error("give a scene_dir path or use --live")
    report = _inspect_bundle(args.scene_dir, args.min)
    if not report:
        print("No non-space cells in scene.")
        return 0
    print(report.format())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
