"""Assert that a scene's server render and client capture agree.

For each scene bundle produced by a CI run, this helper opens
``scene.fb.png`` (the server's renderer ground truth) and
``scene.client.png`` (whatever the client-side capture produced —
a ``vncdo capture`` PNG on Linux, a cropped Screen Sharing window
capture on macOS) and decides whether they represent the same
rendered frame.

This is the *only* place in the pipeline that actually answers the
question "did the client see what the server rendered?". The
separate ``tests/crop_and_hash_macos.py`` script is a drift gate
— it compares today's client screenshot against a previously saved
client screenshot and is blind to server↔client divergence within
a single run.

Two-metric stack + cheap guard
==============================

0. **Blank guard.** Client luminance std dev > ``--min-std`` (in
   the 0–255 scale; default 3.0). Catches solid-black or
   all-dark-grey captures with a clear error message before
   spending any cycles on the real metrics. This is the exact
   failure mode we hit when Screen Sharing's window-ID lookup
   returned empty on macos-latest and ``screencapture -x -o``
   fell back to capturing an all-black desktop.

1. **SSIM on grayscale** (``skimage.metrics.structural_similarity``,
   Wang et al. 2004 parameters). The client image is resized to
   the server's shape with anti-aliased lanczos first so the two
   buffers share a coordinate system. SSIM compares local
   luminance/contrast/structure in 11×11 Gaussian windows and is
   robust to gamma/colour-profile shifts and to the exact kind of
   lanczos resampling Screen Sharing applies on top of vncvt's
   raster.

   Thresholds: expect ≥ 0.98 for vncdo loopback (no resample),
   0.85–0.95 for Screen Sharing (lanczos + retina + colour
   profile), ≤ 0.4 for cross-content pairs. Default hard-fail at
   ``--ssim-min 0.70`` sits squarely in the gap.

2. **HSV histogram Bhattacharyya distance.** Orthogonal to SSIM:
   captures "what colours are in the image, in what proportion"
   without caring about spatial structure. vncvt frames are ~100%
   black and amber; anything with a different palette (Dock
   icons, grey Connecting-dialog chrome, browser window) produces
   distance ≫ 0.5 regardless of resampling.

   Thresholds: same-content pairs (even with resampling) land at
   ≤ 0.3; cross-content pairs at ≥ 0.6. Default hard-fail at
   ``--bhat-max 0.50``.

Both signals must pass; either one firing is enough to fail the
check. They are uncorrelated enough that the joint false-positive
rate on a principled calibration set would be negligible.

Calibration note
================

The 0.70 / 0.50 defaults come from the published literature
(scikit-image SSIM examples, Wang et al. 2004, and OpenCV's
histogram-comparison tutorial for Bhattacharyya ranges). They are
*defensible defaults*, not CI-pipeline-calibrated values.

The right long-term solution is to pin a small
``tests/scene_calibration/`` corpus of known-good and known-bad
scene pairs, compute the metric distribution on first push, and
set thresholds at percentile-based bounds (e.g. 1st percentile of
SSIM_same, 99th percentile of Bhattacharyya_same). That can land
in a follow-up commit once we have a corpus of real Screen
Sharing captures to calibrate against.

Usage:
    python tests/check_scene_match.py <scene_dir> [options]

Exit codes:
    0 — all checks passed
    1 — at least one check failed (see stderr for which)
    2 — missing file or invalid input

Dependencies:
    PIL (Pillow), numpy, scikit-image. CI invokes this via
    ``uv run --with scikit-image --with pillow`` so the deps are
    ephemeral and not part of the vncvt package dep set.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

try:
    from skimage.metrics import structural_similarity as ssim
    from skimage.transform import resize as sk_resize
except ImportError as e:  # pragma: no cover — dependency shape error
    print(
        f"ERROR: scikit-image is required. Install with "
        f"`uv run --with scikit-image --with pillow python "
        f"tests/check_scene_match.py ...` (CI does this). "
        f"Import error: {e}",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _load_gray_01(path: Path) -> np.ndarray:
    """Load an image as a float32 grayscale array normalised to [0, 1]."""
    return np.asarray(Image.open(path).convert("L"), dtype=np.float32) / 255.0


def _load_hsv(path: Path) -> np.ndarray:
    """Load an image as an HSV uint8 array."""
    return np.asarray(Image.open(path).convert("HSV"))


def _hs_hist(hsv: np.ndarray, bins: tuple[int, int] = (32, 32)) -> np.ndarray:
    """Normalised 2D hue-saturation histogram."""
    h = hsv[..., 0].ravel()
    s = hsv[..., 1].ravel()
    hist, _, _ = np.histogram2d(h, s, bins=bins, range=[[0, 256], [0, 256]])
    total = hist.sum()
    if total <= 0:
        return hist
    return hist / total


def _bhattacharyya(p: np.ndarray, q: np.ndarray) -> float:
    """Bhattacharyya distance between two normalised distributions.

    Bounded in [0, 1]; 0 = identical, 1 = disjoint support.
    """
    bc = float(np.sum(np.sqrt(p * q)))
    return float(np.sqrt(max(0.0, 1.0 - bc)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "scene_dir",
        type=Path,
        help="Directory containing scene.fb.png and scene.client.png",
    )
    parser.add_argument(
        "--ssim-min",
        type=float,
        default=0.70,
        help="Minimum acceptable SSIM score (default: 0.70). Typical "
             "same-content pairs score >= 0.85 even after lanczos "
             "resampling; cross-content pairs collapse to <= 0.5.",
    )
    parser.add_argument(
        "--bhat-max",
        type=float,
        default=0.50,
        help="Maximum acceptable HSV Bhattacharyya distance "
             "(default: 0.50). Typical same-palette pairs <= 0.3; "
             "cross-palette pairs >= 0.6.",
    )
    parser.add_argument(
        "--min-std",
        type=float,
        default=3.0,
        help="Minimum client luminance std dev in the 0-255 scale "
             "(default: 3.0). Lower values indicate a blank/uniform "
             "capture.",
    )
    args = parser.parse_args()

    fb_path = args.scene_dir / "scene.fb.png"
    client_path = args.scene_dir / "scene.client.png"

    if not fb_path.exists():
        print(f"ERROR: {fb_path} not found", file=sys.stderr)
        return 2
    if not client_path.exists():
        print(f"ERROR: {client_path} not found", file=sys.stderr)
        return 2

    fb_gray = _load_gray_01(fb_path)
    client_gray = _load_gray_01(client_path)

    # Align client to server coordinate system. Screen Sharing
    # captures at retina resolution and crop artefacts may leave
    # the client image a different shape; SSIM requires both
    # inputs to be identically shaped.
    if client_gray.shape != fb_gray.shape:
        client_gray_resized = sk_resize(
            client_gray, fb_gray.shape, anti_aliasing=True
        )
    else:
        client_gray_resized = client_gray

    # Read the HSV histograms from the original (unresized) client
    # image — resizing in grayscale loses colour information, and
    # histograms are resolution-invariant anyway.
    fb_hist = _hs_hist(_load_hsv(fb_path))
    client_hist = _hs_hist(_load_hsv(client_path))

    # Metrics
    client_std_255 = float(client_gray.std() * 255.0)
    ssim_score = float(
        ssim(
            fb_gray,
            client_gray_resized,
            data_range=1.0,
            gaussian_weights=True,
            sigma=1.5,
            use_sample_covariance=False,
        )
    )
    bhat = _bhattacharyya(fb_hist, client_hist)

    # Sizes for the diagnostic log — the raw client size is useful
    # for debugging Screen Sharing crop problems.
    with Image.open(fb_path) as _im:
        fb_size = _im.size
    with Image.open(client_path) as _im:
        client_size = _im.size

    print(f"scene_dir:    {args.scene_dir}")
    print(f"fb:           size={fb_size}")
    print(f"client:       size={client_size} std_255={client_std_255:.2f}")
    print(f"SSIM:         {ssim_score:.4f}  (min {args.ssim_min})")
    print(f"Bhattacharyya:{bhat:.4f}  (max {args.bhat_max})")

    errors: list[str] = []

    if client_std_255 < args.min_std:
        errors.append(
            f"CLIENT IS BLANK: luminance std dev {client_std_255:.2f} "
            f"< min {args.min_std}. The client capture contains no "
            f"meaningful image content — most likely the VNC client "
            f"never painted a frame (e.g. still on a connection "
            f"progress dialog, or the screencapture fallback hit an "
            f"all-black desktop)."
        )

    if ssim_score < args.ssim_min:
        errors.append(
            f"SSIM {ssim_score:.4f} below minimum {args.ssim_min}. "
            f"The server and client frames are structurally "
            f"different images even after aligning to the same "
            f"resolution — the client is showing something other "
            f"than the terminal output."
        )

    if bhat > args.bhat_max:
        errors.append(
            f"HSV Bhattacharyya distance {bhat:.4f} above maximum "
            f"{args.bhat_max}. The colour palette of the client "
            f"capture does not match the server render (vncvt is "
            f"black + amber; the client is showing something with "
            f"a different palette such as a macOS dialog, browser "
            f"chrome, or dock icons)."
        )

    if errors:
        print("", file=sys.stderr)
        print(
            "FAIL — server↔client scene verification failed:",
            file=sys.stderr,
        )
        for e in errors:
            print(f"  * {e}", file=sys.stderr)
        return 1

    print("PASS: server and client scene frames agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
