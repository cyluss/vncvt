"""Crop a Screen Sharing window screenshot and run imagehash on it.

This script is only invoked by the ``macos-screen-sharing`` CI job.
It expects ``screenshot-mac-window.png`` in the current directory
(produced by ``screencapture -l <windowID>``), strips the macOS title
bar and the 1px window border, computes a dHash, and compares it to
``tests/baselines/baselines-macos.json`` with a looser tolerance than
the Linux suite because Screen Sharing does its own client-side
resampling on top of vncvt's raster.

Usage:
    uv run --with imagehash --with pillow python tests/crop_and_hash_macos.py

Exits non-zero if the dHash drift exceeds ``TOLERANCE``. On the very
first run (no committed baseline yet) the helper writes the baseline
and exits 0 — commit the generated file to lock it in.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image
import imagehash

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import baselines as b  # noqa: E402  (sys.path mutated)

SOURCE = Path("screenshot-mac-window.png")
CROPPED = Path("screenshot-mac-cropped.png")
TITLE_BAR_PX = 28
BORDER_PX = 1
TOLERANCE = 12
BASELINE_NAME = "macos_screen_sharing_initial"


def main() -> int:
    if not SOURCE.exists():
        print(f"ERROR: {SOURCE} not found", file=sys.stderr)
        return 2

    img = Image.open(SOURCE).convert("RGB")
    w, h = img.size
    if h <= TITLE_BAR_PX + BORDER_PX:
        print(f"ERROR: window capture too small: {img.size}", file=sys.stderr)
        return 2

    cropped = img.crop((BORDER_PX, TITLE_BAR_PX, w - BORDER_PX, h - BORDER_PX))
    cropped.save(CROPPED)
    print(f"Source size:  {img.size}")
    print(f"Cropped size: {cropped.size}")

    # Point the baselines module at the macOS-specific file so it
    # doesn't collide with the Linux baseline.
    b._BASELINE_FILE = _HERE / "baselines" / "baselines-macos.json"

    current = imagehash.dhash(cropped, hash_size=16)
    print(f"dHash:        {current}")

    existing = b.load_hash(BASELINE_NAME)
    if existing is None:
        b.save_hash(BASELINE_NAME, current)
        print(
            f"No baseline found — wrote {BASELINE_NAME} = {current} to "
            f"{b._BASELINE_FILE}. Commit this file to lock it in."
        )
        return 0

    distance = current - existing
    print(f"Baseline:     {existing}")
    print(f"Distance:     {distance} (tolerance {TOLERANCE})")
    if distance > TOLERANCE:
        print(
            f"FAIL: dHash distance {distance} exceeds tolerance {TOLERANCE} "
            f"for {BASELINE_NAME!r}",
            file=sys.stderr,
        )
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
