"""Subprocess supervisor for spawning vncvt with guaranteed teardown.

Lifted out of ``tests/conftest.py`` so non-test code can reuse the
same lifecycle: a manual debug script, an integration test, or any
caller that wants ``with vncvt_session(...) as srv: ...`` semantics
without reimplementing the readiness-wait and two-phase kill.

Two layers:

- ``start_vncvt`` / ``stop_vncvt`` — raw handle API. Used by the
  pytest fixtures, which need their own yield-based teardown
  plumbing and can't sit inside a context manager.
- ``vncvt_session`` — ``contextmanager`` wrapping the above for code
  that just wants RAII-style cleanup.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_listen(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(
        f"{host}:{port} did not accept connections within {timeout}s"
    )


@dataclass
class VncvtHandle:
    """Per-spawn handle. ``__iter__`` yields ``(host, port)`` for the
    ``host, port = handle`` unpacking pattern used by older tests."""

    host: str
    port: int
    proc: subprocess.Popen
    scene_dir: Path
    control_socket: Path
    _socket_dir: Path = field(repr=False)
    _stderr_fp: object = field(default=None, repr=False)

    def __iter__(self) -> Iterator[object]:
        yield self.host
        yield self.port


def start_vncvt(
    *extra_args: str,
    scene_root: Path,
    port: int | None = None,
    log_traffic: bool = True,
    log_rfb: bool = True,
) -> VncvtHandle:
    """Spawn ``python -m vncvt`` and wait for it to be ready.

    Creates the scene directory and a short-named control-socket dir
    under ``/tmp`` (macOS's 104-byte ``sun_path`` limit makes pytest's
    ``tmp_path`` unsuitable for the socket itself). Returns once the
    RFB listener accepts a TCP connection AND the control socket file
    has appeared. Raises ``RuntimeError`` if either doesn't happen
    within the timeout.

    The caller owns the returned handle and is responsible for calling
    ``stop_vncvt`` on it — use ``vncvt_session`` for automatic cleanup.
    """
    if port is None:
        port = _free_port()
    scene_root = Path(scene_root)
    scene_root.mkdir(parents=True, exist_ok=True)

    socket_dir = Path(tempfile.mkdtemp(prefix="vncvt-ctl-", dir="/tmp"))
    control_socket = socket_dir / "s"

    args = [
        "uv", "run", "python", "-m", "vncvt",
        "--port", str(port),
        "--scene-dump-dir", str(scene_root),
        "--scene-control-socket", str(control_socket),
        *(["--log-traffic"] if log_traffic else []),
        *extra_args,
    ]
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    if log_rfb:
        env["VNCVT_LOG_RFB"] = "1"
    # Suppress zsh session-restore banner so tests get a reproducible
    # initial prompt. On macOS, zsh login shells dump a line like
    # "/Users/kang/.zsh_sessions/<UUID>.session:N: command not found:
    # Saving" before the first prompt — the UUID is machine-specific,
    # which wrecks dHash-based baseline comparisons across machines.
    env["SHELL_SESSIONS_DISABLE"] = "1"
    env["SHELL_SESSION_DIR"] = ""
    env.pop("SHELL_SESSION_ID", None)

    # Stream stderr to a log file under scene_root so the RFB trace
    # (the only diagnostic we have when something goes wrong end-to-
    # end) gets uploaded with the scene-dump artifact. Piping to a
    # PIPE we don't drain would deadlock the child once the OS
    # buffer fills.
    stderr_log = scene_root / "vncvt-server.log"
    stderr_fp = open(stderr_log, "wb")
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=stderr_fp,
        env=env,
    )
    try:
        _wait_for_listen("127.0.0.1", port)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if control_socket.exists():
                break
            time.sleep(0.05)
    except Exception:
        proc.kill()
        stderr_fp.close()
        shutil.rmtree(socket_dir, ignore_errors=True)
        raise

    return VncvtHandle(
        host="127.0.0.1",
        port=port,
        proc=proc,
        scene_dir=scene_root,
        control_socket=control_socket,
        _socket_dir=socket_dir,
        _stderr_fp=stderr_fp,
    )


def stop_vncvt(handle: VncvtHandle) -> None:
    """Two-phase shutdown: SIGTERM, wait 2s, SIGKILL if still alive.

    Idempotent — calling it again on an already-dead process is a
    no-op. Always rmtrees the socket directory so the caller doesn't
    have to remember.
    """
    proc = handle.proc
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
    if handle._stderr_fp is not None:
        try:
            handle._stderr_fp.close()
        except Exception:
            pass
    shutil.rmtree(handle._socket_dir, ignore_errors=True)


@contextmanager
def vncvt_session(
    *extra_args: str,
    scene_root: Path,
    port: int | None = None,
    log_traffic: bool = True,
    log_rfb: bool = True,
) -> Iterator[VncvtHandle]:
    """RAII wrapper around ``start_vncvt`` / ``stop_vncvt``."""
    handle = start_vncvt(
        *extra_args,
        scene_root=scene_root,
        port=port,
        log_traffic=log_traffic,
        log_rfb=log_rfb,
    )
    try:
        yield handle
    finally:
        stop_vncvt(handle)
