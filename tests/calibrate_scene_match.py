"""Calibrate SSIM + Bhattacharyya thresholds from a corpus of scene pairs.

Generates same-content pairs (server + client view of the same screen)
and cross-content pairs (server + client view of different screens),
computes SSIM and Bhattacharyya distances for each, and prints the
recommended thresholds:

    --ssim-min = min(SSIM_same) - 0.05  (or 1st percentile)
    --bhat-max = max(Bhat_same) + 0.05  (or 99th percentile)

The output also gaps the same/cross distributions visually so you can
see whether the thresholds genuinely separate the two populations.

Linux path only — requires vncdotool. The macOS Screen Sharing side
needs a real GUI session and can't be automated here; hand-curate
that corpus separately.

Usage:
    uv run --with vncdotool --with scikit-image --with pillow \\
        python tests/calibrate_scene_match.py --out tests/scene_calibration/
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent))

from tests.check_scene_match import verify_scene, SceneMismatch  # noqa: E402
from tests.scenes import trigger_server_dump  # noqa: E402
from vncvt.supervisor import start_vncvt, stop_vncvt  # noqa: E402
import asyncio  # noqa: E402


# Scenes we drive the shell through. Each tuple is (scene_name, commands).
_SCENES = [
    ("empty", []),
    ("ls_root", ["ls -la /", ""]),
    ("env_subset", ["env | head -20", ""]),
    ("date", ["date", ""]),
    ("uname", ["uname -a", ""]),
    ("echo_long", ["echo " + "x" * 60, ""]),
    ("pwd", ["pwd", ""]),
    ("history", ["history | head", ""]),
    ("printf_rainbow", [
        "for i in 1 2 3 4 5 6 7; do printf '\\033[%dm█%d\\033[0m ' $((30+i)) $i; done; echo",
        "",
    ]),
    ("multiline", ["echo line1 && echo line2 && echo line3", ""]),
]


def _run_vncdo(host: str, port: int, *ops: str) -> None:
    args = [
        "uv", "run", "--with", "vncdotool", "vncdo",
        "-s", f"{host}::{port}", *ops,
    ]
    subprocess.run(args, check=True, capture_output=True)


def _drive_and_capture(host: str, port: int, commands: list[str], capture: Path) -> None:
    """Type each command + press enter, then capture the client framebuffer."""
    for cmd in commands:
        if cmd:
            _run_vncdo(host, port, "type", cmd)
        _run_vncdo(host, port, "key", "enter")
        time.sleep(0.3)
    time.sleep(0.5)
    _run_vncdo(host, port, "capture", str(capture))


@contextmanager
def _vncvt_session(scene_root: Path):
    handle = start_vncvt(scene_root=scene_root)
    try:
        yield handle
    finally:
        stop_vncvt(handle)


def _dump_sync(control_socket: Path, name: str) -> Path:
    return asyncio.run(trigger_server_dump(control_socket, name))


def build_corpus(out_dir: Path) -> None:
    """Drive each scene, capture both sides, save as a paired bundle."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pairs_dir = out_dir / "pairs"
    pairs_dir.mkdir(exist_ok=True)

    for scene_name, commands in _SCENES:
        scene_root = out_dir / f"session-{scene_name}"
        if scene_root.exists():
            shutil.rmtree(scene_root)
        scene_root.mkdir(parents=True)

        with _vncvt_session(scene_root / "scenes") as srv:
            client_png = scene_root / "client.png"
            _drive_and_capture(srv.host, srv.port, commands, client_png)
            scene_dir = _dump_sync(srv.control_socket, scene_name)
            shutil.copy(client_png, scene_dir / "scene.client.png")

            # Copy the bundle into pairs/ with a stable name
            target = pairs_dir / scene_name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(scene_dir, target)
            print(f"  built pair: {scene_name}")


def _metrics_for(scene_dir: Path) -> dict:
    """Run verify_scene with lenient thresholds so we get back metrics
    even when the pair is known-bad (cross pairs)."""
    try:
        return verify_scene(scene_dir, ssim_min=-1.0, bhat_max=2.0, min_std=0.0)
    except SceneMismatch as e:
        return e.metrics  # type: ignore[attr-defined]


def compute_distributions(out_dir: Path) -> dict:
    """Walk pairs/ and compute SSIM + Bhat for same and cross pairs."""
    import tempfile

    pairs_dir = out_dir / "pairs"
    pair_names = sorted(p.name for p in pairs_dir.iterdir() if p.is_dir())

    same_ssim: list[float] = []
    same_bhat: list[float] = []
    cross_ssim: list[float] = []
    cross_bhat: list[float] = []

    # Same-content pairs: use each bundle's own fb + client
    for name in pair_names:
        m = _metrics_for(pairs_dir / name)
        same_ssim.append(m["ssim"])
        same_bhat.append(m["bhat"])

    # Cross-content pairs: server of A with client of B into a temp dir
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for i, a in enumerate(pair_names):
            for j, b in enumerate(pair_names):
                if i == j:
                    continue
                mix_dir = tmp_path / f"{a}__vs__{b}"
                mix_dir.mkdir()
                shutil.copy(pairs_dir / a / "scene.fb.png", mix_dir / "scene.fb.png")
                shutil.copy(pairs_dir / b / "scene.client.png", mix_dir / "scene.client.png")
                m = _metrics_for(mix_dir)
                cross_ssim.append(m["ssim"])
                cross_bhat.append(m["bhat"])

    return {
        "same_ssim": same_ssim,
        "same_bhat": same_bhat,
        "cross_ssim": cross_ssim,
        "cross_bhat": cross_bhat,
    }


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    sv = sorted(values)
    k = (len(sv) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(sv) - 1)
    return sv[lo] + (sv[hi] - sv[lo]) * (k - lo)


def print_report(dists: dict) -> dict:
    print()
    print("=" * 60)
    print("Calibration report")
    print("=" * 60)

    def _stats(values: list[float]) -> str:
        if not values:
            return "(empty)"
        return (
            f"n={len(values):3d}  "
            f"min={min(values):.4f}  "
            f"p01={_percentile(values, 0.01):.4f}  "
            f"p50={statistics.median(values):.4f}  "
            f"p99={_percentile(values, 0.99):.4f}  "
            f"max={max(values):.4f}"
        )

    print(f"SSIM same:  {_stats(dists['same_ssim'])}")
    print(f"SSIM cross: {_stats(dists['cross_ssim'])}")
    print(f"Bhat same:  {_stats(dists['same_bhat'])}")
    print(f"Bhat cross: {_stats(dists['cross_bhat'])}")
    print()

    # Recommended thresholds
    if dists["same_ssim"] and dists["same_bhat"]:
        rec_ssim = max(0.0, min(dists["same_ssim"]) - 0.05)
        rec_bhat = min(1.0, max(dists["same_bhat"]) + 0.05)
    else:
        rec_ssim = 0.70
        rec_bhat = 0.50

    gap_ssim = (
        min(dists["same_ssim"]) - max(dists["cross_ssim"])
        if dists["same_ssim"] and dists["cross_ssim"]
        else 0.0
    )
    gap_bhat = (
        min(dists["cross_bhat"]) - max(dists["same_bhat"])
        if dists["same_bhat"] and dists["cross_bhat"]
        else 0.0
    )
    print(f"SSIM gap (same-min - cross-max): {gap_ssim:+.4f}")
    print(f"Bhat gap (cross-min - same-max): {gap_bhat:+.4f}")
    print()
    print("Recommended thresholds:")
    print(f"  --ssim-min {rec_ssim:.4f}")
    print(f"  --bhat-max {rec_bhat:.4f}")
    print()

    return {
        **dists,
        "recommended_ssim_min": rec_ssim,
        "recommended_bhat_max": rec_bhat,
        "ssim_gap": gap_ssim,
        "bhat_gap": gap_bhat,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out", type=Path, default=_HERE / "scene_calibration",
        help="Output directory for paired bundles + report.",
    )
    parser.add_argument(
        "--skip-build", action="store_true",
        help="Skip corpus generation, just recompute metrics from existing pairs.",
    )
    args = parser.parse_args()

    if not args.skip_build:
        print(f"Building corpus in {args.out}/pairs/ ...")
        build_corpus(args.out)

    print(f"Computing distributions from {args.out}/pairs/ ...")
    dists = compute_distributions(args.out)
    report = print_report(dists)

    report_path = args.out / "report.json"
    report_path.write_text(json.dumps(report, indent=2))
    print(f"Full report written to {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
