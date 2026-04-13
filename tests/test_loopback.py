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
    host: str, port: int, capture_path: Path, password: str | None = None,
) -> Iterator[Path]:
    """Drive the server with Apple Screen Sharing.app.

    Opens the ``vnc://[password@]host:port`` URL — when ``password``
    is given it's embedded in the URL so Screen Sharing reads it
    directly, no keychain dance and no password dialog. Waits for the
    remote window to appear, types a command via System Events,
    screencaptures the window by its bounds, and quits Screen Sharing
    on exit so the next test starts clean.
    """
    if password is not None:
        # macOS Screen Sharing expects the URL in user:password@host
        # form even though VNC has no username concept; without the
        # colon the prefix is parsed as a bare username and Screen
        # Sharing pops a password dialog instead. Any non-empty
        # username works.
        url = f"vnc://vnc:{password}@{host}:{port}"
    else:
        url = f"vnc://{host}:{port}"
    subprocess.run(["open", url], check=True)
    try:
        x, y, w, h = _wait_for_screen_sharing_window()
        # Bring Screen Sharing to the foreground so screencapture -R
        # actually grabs its window pixels. Without this, screencapture
        # grabs whatever happens to be at that screen region — which
        # on a multi-window environment is usually the desktop or
        # whatever window is on top of Screen Sharing.
        _osascript('tell application "Screen Sharing" to activate')
        time.sleep(1.0)
        _osascript(
            'tell application "System Events" to '
            'keystroke "echo loopback_works"'
        )
        _osascript(
            'tell application "System Events" to key code 36'
        )
        time.sleep(1.0)
        # Re-activate in case the keystrokes shifted focus, and
        # re-read the window bounds in case the user/window-manager
        # moved the window between wait and capture.
        _osascript('tell application "Screen Sharing" to activate')
        time.sleep(0.3)
        x, y, w, h = _wait_for_screen_sharing_window(timeout=3.0)
        # Debug aid: also capture the full screen so we can see where
        # Screen Sharing actually is when the region capture comes up
        # blank. Lives next to the region capture in the artifact dir.
        full_screen = capture_path.with_name("fullscreen.png")
        subprocess.run(
            ["screencapture", "-x", str(full_screen)],
            check=False,
        )
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


_ARTIFACT_ROOT = Path(__file__).parent / "scene-dumps"


def _stable_scene_root(name: str) -> Path:
    """Per-test stable artifact dir. Persists across the test so the
    workflow's upload-artifact step can see what was captured even
    when the scene match assertion fails — pytest's tmp_path is
    cleaned and lives under /private/var/folders, neither of which
    survives to artifact upload."""
    root = _ARTIFACT_ROOT / name
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    return root


def _dump_diagnostics(scene_root: Path, label: str) -> None:
    """Best-effort dump of diagnostic state to the scene root, run in
    a finally block so a failing test still produces an artifact."""
    diag = scene_root / f"{label}.diag.txt"
    try:
        with diag.open("w") as f:
            f.write(f"=== pgrep Screen Sharing ===\n")
            f.write(subprocess.run(
                ["pgrep", "-fl", "Screen Sharing"],
                capture_output=True, text=True,
            ).stdout or "(none)\n")
            f.write(f"\n=== osascript window probe ===\n")
            probe = subprocess.run(
                ["osascript",
                 "-e", 'tell application "System Events"',
                 "-e", '  if not (exists process "Screen Sharing") then return "no process"',
                 "-e", '  tell process "Screen Sharing"',
                 "-e", '    set out to "windows: " & (count of windows) & return',
                 "-e", '    repeat with w in windows',
                 "-e", '      set out to out & "  - name=" & (name of w) & " pos=" & (position of w as text) & " size=" & (size of w as text) & return',
                 "-e", '    end repeat',
                 "-e", '    return out',
                 "-e", '  end tell',
                 "-e", 'end tell'],
                capture_output=True, text=True,
            )
            f.write(probe.stdout or "")
            if probe.stderr:
                f.write(f"\nstderr: {probe.stderr}\n")
    except Exception as e:
        try:
            diag.write_text(f"diagnostics dump failed: {e}\n")
        except Exception:
            pass


@pytest.mark.loopback
@pytest.mark.skipif(IS_MAC, reason="vncdotool driver is the linux loopback")
def test_loopback_vncdo():
    scene_root = _stable_scene_root("loopback-linux")
    capture = scene_root / "client.png"
    with vncvt_session(scene_root=scene_root) as srv:
        with vncdo_driver(srv.host, srv.port, capture):
            scene_dir = _trigger_dump_sync(srv.control_socket, "loopback")
            shutil.copy(capture, scene_dir / "scene.client.png")
            verify_scene(scene_dir, **LINUX_THRESHOLDS)


@pytest.mark.loopback
@pytest.mark.skipif(
    not IS_MAC, reason="Screen Sharing driver is the macOS loopback"
)
def test_loopback_screen_sharing():
    # Run vncvt with VNC auth (security type 2) so the test exercises
    # the real RFB 3.3 challenge/response path that the previous
    # session's dialect fix was about. The password is embedded in
    # the vnc:// URL passed to `open`, which Screen Sharing.app reads
    # directly — no keychain seeding, no password dialog. Verified
    # end-to-end against a real Screen Sharing.app on macOS Monterey.
    scene_root = _stable_scene_root("loopback-macos")
    capture = scene_root / "client.png"
    password = "testpass"
    try:
        with vncvt_session(
            "--password", password, scene_root=scene_root
        ) as srv:
            with screen_sharing_driver(
                srv.host, srv.port, capture, password=password
            ):
                _dump_diagnostics(scene_root, "after_capture")
                scene_dir = _trigger_dump_sync(
                    srv.control_socket, "loopback"
                )
                if capture.exists():
                    shutil.copy(capture, scene_dir / "scene.client.png")
                verify_scene(scene_dir, **MAC_THRESHOLDS)
    except Exception:
        _dump_diagnostics(scene_root, "on_failure")
        raise


def _trigger_dump_sync(control_socket: Path, name: str) -> Path:
    """Synchronous wrapper around the async trigger_server_dump.

    The loopback tests are plain ``def`` tests (not async) because
    they spend almost all their time in subprocess.communicate, so
    making them async would just add ceremony. Run the one async call
    in a fresh event loop.
    """
    import asyncio
    return asyncio.run(trigger_server_dump(control_socket, name))
