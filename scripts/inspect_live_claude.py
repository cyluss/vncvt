#!/usr/bin/env python3
"""Spawn vncvt pointed at a *real* Claude Code project with existing
conversation history, wait for Claude to replay the prior turns
(which is when the dim "previous prompt" styling kicks in), dump the
scene, and run the per-cell contrast inspector on it.

Unlike ``scripts/mock_claude_session.py`` this captures whatever
SGR bytes Claude actually emits for dim history — so we can tell
whether "invisible previous prompts" is the renderer's fault or
just a theme mismatch.

Usage:
    uv run python scripts/inspect_live_claude.py \
        --project-dir ~/some/repo/with/history --theme light
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import asyncvnc

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.inspect_cell_contrast import _inspect_bundle, _report  # noqa: E402
from tests.scenes import trigger_server_dump  # noqa: E402
from vncvt.supervisor import start_vncvt, stop_vncvt  # noqa: E402


async def _run(project_dir: Path, theme: str, threshold: float,
               wait_replay: float) -> int:
    claude = Path.home() / ".local" / "bin" / "claude"
    if not claude.is_file():
        print(f"ERROR: claude binary not found at {claude}", file=sys.stderr)
        return 1
    if not project_dir.is_dir():
        print(f"ERROR: {project_dir} is not a directory", file=sys.stderr)
        return 1

    cc_theme = "light-ansi" if theme == "light" else "dark-ansi"

    with TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        settings = tmpdir / "cc.json"
        settings.write_text(json.dumps({"theme": cc_theme}))
        # Wrapper cd's into the real project dir so claude picks up
        # its existing ~/.claude/projects/<hash>/*.jsonl history.
        wrapper = tmpdir / "cc.sh"
        wrapper.write_text(
            f"#!/bin/bash\n"
            f"cd {project_dir}\n"
            f"exec {claude} --settings {settings} --resume \"$@\"\n"
        )
        wrapper.chmod(0o755)

        handle = start_vncvt(
            "--shell", str(wrapper),
            "--theme", theme,
            scene_root=tmpdir / "scenes",
        )
        try:
            async with asyncvnc.connect(
                host=handle.host, port=handle.port, password="vncvt",
            ) as vnc:
                # Claude's --resume picker needs a second to paint,
                # then Return selects the most recent session.
                await asyncio.sleep(2.0)
                vnc.keyboard.press("Return")
                # Give the replay room to paint dim history
                await asyncio.sleep(wait_replay)
                for _ in range(3):
                    await vnc.screenshot()
                    await asyncio.sleep(0.3)
                scene_dir = await trigger_server_dump(
                    handle.control_socket, "live-claude",
                )
                keep = tmpdir / "keep"
                keep.mkdir()
                shutil.copy(scene_dir / "scene.fb.png", keep / "scene.fb.png")
                shutil.copy(scene_dir / "scene.term.json", keep / "scene.term.json")
                # Also copy somewhere persistent so the user can eyeball it
                out_dir = Path("/tmp/vncvt-live-claude")
                out_dir.mkdir(exist_ok=True)
                shutil.copy(keep / "scene.fb.png", out_dir / f"{theme}.fb.png")
                shutil.copy(keep / "scene.term.json", out_dir / f"{theme}.term.json")
                print(f"scene bundle saved to {out_dir}/{theme}.*")
                print()
        finally:
            stop_vncvt(handle)

        results, stats = _inspect_bundle(keep, threshold)
        _report(results, stats)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-dir", type=Path, required=True,
        help="Path to a dir where `claude --resume` will find history.",
    )
    parser.add_argument(
        "--theme", default="light",
        choices=("amber", "dark", "light", "green", "c64", "dos", "atari"),
    )
    parser.add_argument(
        "--min", type=float, default=4.5,
        help="WCAG threshold for the cell-contrast report (default 4.5).",
    )
    parser.add_argument(
        "--wait-replay", type=float, default=6.0,
        help="Seconds to wait after Return for Claude to replay history.",
    )
    args = parser.parse_args()
    return asyncio.run(_run(
        args.project_dir.expanduser().resolve(),
        args.theme, args.min, args.wait_replay,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
