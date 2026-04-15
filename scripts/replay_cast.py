#!/usr/bin/env python3
"""CLI wrapper around ``vncvt.cast_replay``. Replay a .cast file
through vncvt's renderer and print a per-cell contrast report.

Usage:
    uv run python scripts/replay_cast.py session.cast
    uv run python scripts/replay_cast.py session.cast --theme dark --at 10.5
    uv run python scripts/replay_cast.py session.cast --json /tmp/report.json

See ``vncvt/cast_replay.py`` for the importable API — prefer that
when calling from tests, notebooks, or other scripts.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from vncvt.cast_replay import inspect_frame, replay_cast  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cast_path", type=Path)
    parser.add_argument(
        "--theme", default="light",
        choices=("amber", "dark", "light", "green", "powershell"),
    )
    parser.add_argument("--at", default="end",
                        help="Seconds from start, or 'end' (default).")
    parser.add_argument("--out", type=Path, default=Path("/tmp/replay.png"),
                        help="Where to save the rendered framebuffer PNG.")
    parser.add_argument("--min", type=float, default=4.5,
                        help="WCAG threshold for the report (default 4.5).")
    parser.add_argument("--json", type=Path, default=None,
                        help="Also dump the report as JSON to this path.")
    parser.add_argument("--contrast", default="high",
                        choices=("normal", "high", "max"))
    parser.add_argument("--font-size", type=int, default=13)
    parser.add_argument("--line-height", type=float, default=1.1)
    args = parser.parse_args()

    at: float | str = args.at if args.at == "end" else float(args.at)
    frame = replay_cast(
        args.cast_path, theme=args.theme, at=at,
        font_size=args.font_size, line_height=args.line_height,
        contrast=args.contrast,
    )
    frame.save_png(args.out)
    print(f"saved framebuffer to {args.out}")
    print()

    report = inspect_frame(frame, threshold=args.min)
    print(report.format())

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "cast": str(frame.cast_path),
            "theme": frame.theme,
            "at": frame.at,
            "stats": {
                "count": report.count,
                "min": report.min, "p10": report.p10,
                "median": report.median, "p90": report.p90, "max": report.max,
            },
            "threshold": report.threshold,
            "below_threshold": [asdict(c) for c in report.below_threshold],
        }
        args.json.write_text(json.dumps(doc, indent=2))
        print(f"\njson report → {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
