"""End-to-end loopback test: spawn vncvt, drive a real VNC client,
verify the client sees what the server rendered.

One test per platform — gated by a ``@pytest.mark.loopback`` marker so
the unit-suite job doesn't accidentally try to drive vncdotool or
Screen Sharing.app on every push. CI opts in explicitly with
``pytest -m loopback``.

The supervisor pattern from ``vncvt.supervisor`` owns the server
process; matching ``contextmanager`` driver helpers below own the
client process. Both layers do two-phase teardown so a test failure
never leaks orphan processes.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from vncvt.supervisor import vncvt_session
from .check_scene_match import verify_scene
from .scenes import trigger_server_dump


IS_MAC = sys.platform == "darwin"

LINUX_THRESHOLDS = dict(ssim_min=0.90, bhat_max=0.25, min_std=3.0)
MAC_THRESHOLDS = dict(ssim_min=0.70, bhat_max=0.50, min_std=3.0)


# ---------------------------------------------------------------------------
# Driver supervisors
# ---------------------------------------------------------------------------


@contextmanager
def vncdo_driver(host: str, port: int, capture_path: Path) -> Iterator[Path]:
    """Drive the server with vncdotool and capture its framebuffer.

    vncdotool is a one-shot CLI: it connects, runs the requested ops
    (type/key/capture), exits. The supervisor here is mostly to
    guarantee the subprocess is gone if anything raises before
    ``communicate`` returns.
    """
    proc = subprocess.Popen(
        [
            "uv", "run", "--with", "vncdotool", "vncdo",
            "-s", f"{host}::{port}",
            "type", "echo loopback_works",
            "key", "enter",
            "capture", str(capture_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        try:
            _out, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            _out, err = proc.communicate()
            raise RuntimeError(f"vncdo timed out after 30s: {err.decode()}")
        if proc.returncode != 0:
            raise RuntimeError(
                f"vncdo exited {proc.returncode}: {err.decode()}"
            )
        if not capture_path.exists():
            raise RuntimeError(
                f"vncdo reported success but {capture_path} is missing"
            )
        yield capture_path
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


def _osascript(*lines: str) -> str:
    """Run an AppleScript snippet (one statement per arg) and return stdout."""
    cmd = ["osascript"]
    for line in lines:
        cmd += ["-e", line]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return res.stdout.strip()


def _wait_for_screen_sharing_window(
    timeout: float = 15.0,
) -> tuple[int, int, int, int]:
    """Poll until Screen Sharing.app has a window open, return its
    bounds as (x, y, w, h).

    Screen Sharing.app is officially unscriptable and its windows
    don't expose ``AXIdentifier``, so ``id of window 1`` returns
    empty even when a window exists. Use position + size from
    System Events instead and pass the bounds to
    ``screencapture -R x,y,w,h``.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = _osascript(
            'tell application "System Events"',
            '  if not (exists process "Screen Sharing") then return ""',
            '  tell process "Screen Sharing"',
            '    if (count of windows) is 0 then return ""',
            '    set p to position of window 1',
            '    set s to size of window 1',
            '    return ((item 1 of p) as text) & "," & ((item 2 of p) as text) & "," & ((item 1 of s) as text) & "," & ((item 2 of s) as text)',
            '  end tell',
            'end tell',
        )
        if out:
            try:
                x, y, w, h = (int(v) for v in out.split(","))
                if w > 0 and h > 0:
                    return x, y, w, h
            except ValueError:
                pass
        time.sleep(0.3)
    raise RuntimeError(
        f"Screen Sharing window did not appear within {timeout}s"
    )


@contextmanager
def screen_sharing_driver(
    host: str, port: int, capture_path: Path
) -> Iterator[Path]:
    """Drive the server with Apple Screen Sharing.app.

    Opens the vnc:// URL (auth comes from the seeded keychain entry —
    see the ``loopback`` job in ``.github/workflows/loopback-test.yml``),
    waits for the remote window to appear, types a command via System
    Events, screencaptures the window, and quits Screen Sharing on
    exit so the next test starts clean.
    """
    subprocess.run(["open", f"vnc://{host}:{port}"], check=True)
    try:
        x, y, w, h = _wait_for_screen_sharing_window()
        # Give Screen Sharing a beat to actually paint the framebuffer
        # after the window appears — the window exists before the
        # remote raster has been drawn.
        time.sleep(1.0)
        _osascript(
            'tell application "System Events" to '
            'keystroke "echo loopback_works"'
        )
        _osascript(
            'tell application "System Events" to key code 36'
        )
        time.sleep(1.0)
        subprocess.run(
            [
                "screencapture", "-x",
                "-R", f"{x},{y},{w},{h}",
                str(capture_path),
            ],
            check=True,
        )
        if not capture_path.exists():
            raise RuntimeError(
                f"screencapture reported success but {capture_path} is missing"
            )
        yield capture_path
    finally:
        subprocess.run(
            ["osascript", "-e",
             'tell application "Screen Sharing" to quit'],
            check=False,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.loopback
@pytest.mark.skipif(IS_MAC, reason="vncdotool driver is the linux loopback")
def test_loopback_vncdo(tmp_path):
    scene_root = tmp_path / "scenes"
    capture = tmp_path / "client.png"
    with vncvt_session(scene_root=scene_root) as srv:
        with vncdo_driver(srv.host, srv.port, capture):
            scene_dir = _trigger_dump_sync(srv.control_socket, "loopback")
            shutil.copy(capture, scene_dir / "scene.client.png")
            verify_scene(scene_dir, **LINUX_THRESHOLDS)


@pytest.mark.loopback
@pytest.mark.skipif(
    not IS_MAC, reason="Screen Sharing driver is the macOS loopback"
)
def test_loopback_screen_sharing(tmp_path):
    scene_root = tmp_path / "scenes"
    capture = tmp_path / "client.png"
    with vncvt_session(
        "--password", "testpass", scene_root=scene_root
    ) as srv:
        with screen_sharing_driver(srv.host, srv.port, capture):
            scene_dir = _trigger_dump_sync(srv.control_socket, "loopback")
            shutil.copy(capture, scene_dir / "scene.client.png")
            verify_scene(scene_dir, **MAC_THRESHOLDS)


def _trigger_dump_sync(control_socket: Path, name: str) -> Path:
    """Synchronous wrapper around the async trigger_server_dump.

    The loopback tests are plain ``def`` tests (not async) because
    they spend almost all their time in subprocess.communicate, so
    making them async would just add ceremony. Run the one async call
    in a fresh event loop.
    """
    import asyncio
    return asyncio.run(trigger_server_dump(control_socket, name))
