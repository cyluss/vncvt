"""Sanity test for the scene-match calibration script.

The real calibration corpus is generated on demand by
``tests/calibrate_scene_match.py`` (not committed — outputs depend on
machine state). This test runs a tiny 3-scene calibration and asserts
the same-vs-cross SSIM gap is strictly positive, proving that scene-
match thresholds can actually separate matches from non-matches.

Marked as `loopback` because it needs a running vncvt + vncdotool.
Skipped on macOS because vncdotool isn't the Mac loopback driver.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


IS_MAC = sys.platform == "darwin"


@pytest.mark.loopback
@pytest.mark.skipif(IS_MAC, reason="calibration uses vncdotool (linux path)")
def test_calibration_produces_positive_ssim_gap(tmp_path):
    """Run a mini calibration and verify same-min > cross-max in SSIM."""
    # Import here so the module isn't required for non-loopback runs
    from . import calibrate_scene_match as cal

    # Override the scene list with a small set for speed
    cal._SCENES = [
        ("empty", []),
        ("echo_x", ["echo xxxxxxxxxx", ""]),
        ("echo_y", ["echo yyyyyyyyyy", ""]),
    ]
    cal.build_corpus(tmp_path)
    dists = cal.compute_distributions(tmp_path)

    assert len(dists["same_ssim"]) == 3
    assert len(dists["cross_ssim"]) == 6  # 3 * 2 cross pairs

    same_min = min(dists["same_ssim"])
    cross_max = max(dists["cross_ssim"])
    gap = same_min - cross_max
    assert gap > 0, (
        f"calibration failed to separate same/cross SSIM populations: "
        f"same_min={same_min:.4f} cross_max={cross_max:.4f} gap={gap:.4f}"
    )
