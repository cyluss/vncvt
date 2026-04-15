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


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(rgb: tuple[int, int, int]) -> float:
    return (
        0.2126 * _srgb_to_linear(rgb[0] / 255.0)
        + 0.7152 * _srgb_to_linear(rgb[1] / 255.0)
        + 0.0722 * _srgb_to_linear(rgb[2] / 255.0)
    )


def _wcag(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    l1, l2 = _luminance(fg), _luminance(bg)
    return (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)


def _inspect_bundle(
    scene_dir: Path, threshold: float,
) -> tuple[list[tuple[int, int, str, str, str, float]], dict]:
    """Return (below_threshold_cells, stats) for a scene bundle.

    Walks every non-space cell, measures the actual rendered contrast
    between the glyph's peak-delta pixel and the cell corner (bg).
    Returns cells < ``threshold`` plus min/median/max stats across
    ALL non-space cells (not just the ones below threshold)."""
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
    cells = term.get("cells", {})

    padding = 5  # TerminalRenderer.PADDING
    cell_w = (img.width - 2 * padding) / cols
    cell_h = (img.height - 2 * padding) / rows

    results: list[tuple[int, int, str, str, str, float]] = []
    all_ratios: list[float] = []
    for key, cell in cells.items():
        ch = cell.get("c", "")
        if not ch or ch == " ":
            continue
        col_s, row_s = key.split(",")
        col, row = int(col_s), int(row_s)
        x0 = int(round(col * cell_w)) + padding
        y0 = int(round(row * cell_h)) + padding
        x1 = int(round((col + 1) * cell_w)) + padding
        y1 = int(round((row + 1) * cell_h)) + padding
        crop = img.crop((x0, y0, x1, y1))
        raw = crop.tobytes()

        # Corner pixels = bg sample (usually empty areas in the cell)
        bg = (raw[0], raw[1], raw[2])
        # fg sample = the pixel with the MOST different luminance from bg
        bg_lum = _luminance(bg)
        best_px = bg
        best_delta = 0.0
        for i in range(0, len(raw), 3):
            px = (raw[i], raw[i + 1], raw[i + 2])
            d = abs(_luminance(px) - bg_lum)
            if d > best_delta:
                best_delta = d
                best_px = px
        ratio = _wcag(best_px, bg)
        all_ratios.append(ratio)
        if ratio < threshold:
            results.append((
                col, row, ch,
                "#{:02x}{:02x}{:02x}".format(*best_px),
                "#{:02x}{:02x}{:02x}".format(*bg),
                ratio,
            ))
    all_ratios.sort()
    if all_ratios:
        stats = {
            "count": len(all_ratios),
            "min": all_ratios[0],
            "median": all_ratios[len(all_ratios) // 2],
            "max": all_ratios[-1],
            "p10": all_ratios[len(all_ratios) // 10],
            "p90": all_ratios[len(all_ratios) * 9 // 10],
        }
    else:
        stats = {"count": 0}
    return results, stats


def _report(results: list, stats: dict) -> None:
    if stats.get("count", 0) == 0:
        print("No non-space cells in scene.")
        return
    print(f"Cell contrast stats ({stats['count']} non-space cells):")
    print(f"  min    = {stats['min']:6.2f}:1")
    print(f"  p10    = {stats['p10']:6.2f}:1")
    print(f"  median = {stats['median']:6.2f}:1")
    print(f"  p90    = {stats['p90']:6.2f}:1")
    print(f"  max    = {stats['max']:6.2f}:1")
    print()
    if not results:
        print("All cells clear the contrast threshold.")
        return
    print(f"{len(results)} cells below threshold:")
    print(f"  {'col':>4} {'row':>4}  {'char':6}  {'fg':8}  {'bg':8}  {'ratio':>8}")
    for col, row, ch, fg, bg, ratio in sorted(results, key=lambda r: r[5]):
        print(f"  {col:>4} {row:>4}  {repr(ch):6}  {fg}  {bg}  {ratio:>7.2f}:1")


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

        results, stats = _inspect_bundle(keep, threshold)
        _report(results, stats)
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
    results, stats = _inspect_bundle(args.scene_dir, args.min)
    _report(results, stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
