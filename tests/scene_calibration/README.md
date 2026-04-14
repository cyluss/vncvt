# Scene-match calibration corpus

This directory holds calibration artifacts for `tests/check_scene_match.py`'s
SSIM + Bhattacharyya thresholds. The goal: set thresholds from actual
same-vs-cross pair distributions instead of picking literature defaults
and hoping they work.

## How it was built

Run the corpus generator:

```
uv run --with vncdotool --with scikit-image --with pillow \
    python tests/calibrate_scene_match.py --out tests/scene_calibration/
```

The generator spawns a fresh vncvt for each of 10 curated shell scenes
(`empty`, `ls_root`, `env_subset`, `date`, `uname`, `echo_long`, `pwd`,
`history`, `printf_rainbow`, `multiline`), drives each one to a stable
frame via vncdotool, dumps the server-side scene bundle, and captures
the client-side framebuffer. That's 10 same-content pairs.

It then computes 90 cross-content pairs (each server-scene paired with
every other client-scene) and prints SSIM and Bhattacharyya
distributions for same and cross populations.

## Linux results (`report-linux.json`)

With vncdotool as the client, the distributions are:

```
SSIM same:  all 1.0000 across 10 scenes (vncdo is pixel-perfect —
                                          no client-side resampling)
SSIM cross: 0.44 - 0.98 across 90 pairs
Bhat same:  all 0.0000 (identical color histograms)
Bhat cross: 0.00 - 0.02
```

Recommended thresholds from the analysis (`min(same) - 0.05`):

- `ssim_min = 0.95`
- `bhat_max = 0.05`

The existing hardcoded thresholds in `tests/test_loopback.py` are
`ssim_min=0.90 / bhat_max=0.25` — deliberately looser than the
calibration recommendation to absorb CI-to-CI font rendering drift
that local runs don't see. No change needed.

## macOS results

Not yet built. Screen Sharing.app needs a logged-in GUI session and
can't be driven headlessly, so the macOS calibration corpus must be
generated on a real Mac with a human present to click through the
Connect dialog. See `docs/session-handoff-2026-04-13.md` Step 2 for
the procedure.
