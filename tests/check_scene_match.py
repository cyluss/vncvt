"""Assert that a scene's server render and client capture agree.

For each scene bundle produced by a CI run, this helper opens
``scene.fb.png`` (the server's renderer ground truth) and
``scene.client.png`` (whatever the client-side capture produced —
a ``vncdo capture`` PNG on Linux, a cropped Screen Sharing window
capture on macOS) and decides whether they represent the same
rendered frame.

This is the *only* place in the pipeline that actually answers the
question "did the client see what the server rendered?". The
separate ``tests/crop_and_hash_macos.py`` script is a drift gate —
it compares today's client screenshot against a previously saved
client screenshot and is blind to server↔client divergence within
a single run.

Three independent checks run on every invocation, because no
single perceptual metric is robust to the edge cases we've hit:

1. **Blank guard.** Client luminance std dev must be >= ``--min-std``
   (default 3.0). A solid-black or all-dark-grey capture has std
   near 0 and indicates the client never painted anything — for
   example, Screen Sharing sitting on its "Connecting..." sheet
   before the RFB handshake completes. (On vncvt-2026-04-13 this
   manifested as a screenshot of the macOS Dock + a progress
   dialog, captured via the full-desktop fallback when the Screen
   Sharing window-ID lookup returned empty.)

2. **Content-density match.** ``|fb_std - client_std|`` must fit
   inside a band around the server's std dev. Specifically the
   allowed delta is ``max(--std-abs-slack, --std-rel-slack *
   fb_std)`` — so for sparse scenes (small fb_std) we allow an
   absolute cushion, and for dense scenes we allow a relative
   cushion. This catches "client is showing content but it's the
   wrong content" — e.g. a bright Connecting dialog against the
   mostly-dark terminal frame the server rendered.

3. **dHash distance.** With ``hash_size=8`` (64-bit hash), typical
   distances are:
     - Linux vncdo loopback:       ~0-5   (use --tolerance 8)
     - macOS Screen Sharing:       ~5-15  (use --tolerance 20)
     - wildly different images:    30+

Usage:
    python tests/check_scene_match.py <scene_dir> [options]

Exit codes:
    0 — all three checks passed
    1 — at least one check failed (see stderr for which)
    2 — missing file or invalid input
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageStat
import imagehash


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "scene_dir",
        type=Path,
        help="Directory containing scene.fb.png and scene.client.png",
    )
    parser.add_argument(
        "--tolerance",
        type=int,
        default=8,
        help="Max allowed dHash Hamming distance (default: 8)",
    )
    parser.add_argument(
        "--hash-size",
        type=int,
        default=8,
        help="imagehash dhash hash_size (default: 8 -> 64-bit hash)",
    )
    parser.add_argument(
        "--min-std",
        type=float,
        default=3.0,
        help="Minimum client luminance std dev; lower = blank capture "
             "(default: 3.0)",
    )
    parser.add_argument(
        "--std-abs-slack",
        type=float,
        default=5.0,
        help="Absolute slack on content-density delta (default: 5.0)",
    )
    parser.add_argument(
        "--std-rel-slack",
        type=float,
        default=0.75,
        help="Relative slack on content-density delta, fraction of "
             "fb_std (default: 0.75)",
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

    fb_img = Image.open(fb_path).convert("RGB")
    client_img = Image.open(client_path).convert("RGB")

    fb_std = ImageStat.Stat(fb_img.convert("L")).stddev[0]
    client_std = ImageStat.Stat(client_img.convert("L")).stddev[0]

    fb_hash = imagehash.dhash(fb_img, hash_size=args.hash_size)
    client_hash = imagehash.dhash(client_img, hash_size=args.hash_size)
    distance = fb_hash - client_hash

    print(f"scene_dir:    {args.scene_dir}")
    print(f"fb:           size={fb_img.size} std={fb_std:.2f}")
    print(f"client:       size={client_img.size} std={client_std:.2f}")
    print(f"fb dhash:     {fb_hash}")
    print(f"client dhash: {client_hash}")
    print(f"distance:     {distance} (tolerance {args.tolerance})")

    errors: list[str] = []

    # Check 1 — blank guard.
    if client_std < args.min_std:
        errors.append(
            f"CLIENT IS BLANK: luminance std dev {client_std:.2f} < "
            f"min {args.min_std}. The client-side capture contains no "
            f"meaningful image content — most likely the VNC client "
            f"never painted a frame (e.g. still on a connection "
            f"progress dialog, or the screencapture fallback hit an "
            f"all-black desktop)."
        )

    # Check 2 — content-density match.
    allowed_delta = max(args.std_abs_slack, args.std_rel_slack * fb_std)
    density_delta = abs(fb_std - client_std)
    if density_delta > allowed_delta:
        errors.append(
            f"CONTENT DENSITY MISMATCH: |fb_std - client_std| = "
            f"{density_delta:.2f} > allowed {allowed_delta:.2f} "
            f"(max({args.std_abs_slack}, {args.std_rel_slack} * "
            f"{fb_std:.2f})). The client image has very different "
            f"overall busyness from what the server rendered — most "
            f"likely the client is showing something other than the "
            f"terminal output, such as an auth dialog, a connection "
            f"progress sheet, or a desktop wallpaper."
        )

    # Check 3 — dHash distance.
    if distance > args.tolerance:
        errors.append(
            f"DHASH MISMATCH: distance {distance} exceeds tolerance "
            f"{args.tolerance}. The client and server frames are "
            f"structurally different images even after normalising "
            f"for resolution."
        )

    if errors:
        print("", file=sys.stderr)
        print("FAIL — server↔client scene verification failed:", file=sys.stderr)
        for e in errors:
            print(f"  * {e}", file=sys.stderr)
        return 1

    print("PASS: server and client scene frames agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
