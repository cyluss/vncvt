"""Shared pytest fixtures for the vncvt regression suite.

One fixture spawns `vncvt` on a free port and tears it down cleanly.
Another connects an AsyncVNC client to that server.

Tests that need byte-level protocol control talk to the server via
raw sockets and use the same `vncvt_server` fixture for lifecycle.
Tests that want a high-level view (screenshot, type, drag) use
the `vnc` fixture.

The `scene` fixture captures per-scene debugging bundles (text, hex,
fb.png, term.json, client.png) at explicit checkpoints. On test
failure, the pytest_runtest_makereport hook below triggers a
final ``test_failed`` scene automatically.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import asyncvnc
import pytest
import pytest_asyncio

from .scenes import SceneRecorder, trigger_server_dump, capture_client_screenshot


# The final artifact layout (copied out of per-test temp dirs by the
# session teardown) lives here and is uploaded by CI.
_SCENE_ARCHIVE_ROOT = Path(__file__).parent / "scene-dumps"


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
    raise RuntimeError(f"{host}:{port} did not accept connections within {timeout}s")


class VncvtHandle:
    """Per-test handle to a spawned vncvt process with scene dumping."""

    def __init__(
        self,
        host: str,
        port: int,
        proc: subprocess.Popen,
        scene_dir: Path,
        control_socket: Path,
    ) -> None:
        self.host = host
        self.port = port
        self.proc = proc
        self.scene_dir = scene_dir
        self.control_socket = control_socket

    # Allow legacy unpacking: `host, port = vncvt_server`
    def __iter__(self):
        yield self.host
        yield self.port


def _spawn_vncvt(
    extra_args: tuple[str, ...], tmp_path: Path,
) -> VncvtHandle:
    """Start a vncvt subprocess with scene dumping flags wired up.

    Creates a unique scene dir + control socket under ``tmp_path``,
    passes them to the server via CLI, waits for the RFB listener
    to accept, and returns a handle.
    """
    port = _free_port()
    scene_dir = tmp_path / "scenes"
    scene_dir.mkdir(parents=True, exist_ok=True)
    # Unix socket paths must stay under 108 chars on Linux; pytest
    # tmp_path under /tmp is short enough, but use a short name.
    control_socket = tmp_path / "ctl.sock"

    args = [
        "uv", "run", "python", "-m", "vncvt",
        "--port", str(port),
        "--scene-dump-dir", str(scene_dir),
        "--scene-control-socket", str(control_socket),
        *extra_args,
    ]
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    try:
        _wait_for_listen("127.0.0.1", port)
        # Also wait for the control socket file to appear.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if control_socket.exists():
                break
            time.sleep(0.05)
    except Exception:
        proc.kill()
        raise

    return VncvtHandle(
        host="127.0.0.1",
        port=port,
        proc=proc,
        scene_dir=scene_dir,
        control_socket=control_socket,
    )


def _teardown_vncvt(handle: VncvtHandle) -> None:
    handle.proc.terminate()
    try:
        handle.proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        handle.proc.kill()
        handle.proc.wait(timeout=2)


def _archive_scenes(handle: VncvtHandle, archive_subdir: str) -> None:
    """Copy this handle's scene dumps into the committed archive root
    under ``scene-dumps/<archive_subdir>/`` for CI upload."""
    if not handle.scene_dir.exists():
        return
    dest = _SCENE_ARCHIVE_ROOT / archive_subdir
    if dest.exists():
        shutil.rmtree(dest)
    # Only copy if there's something to copy.
    entries = list(handle.scene_dir.iterdir())
    if not entries:
        return
    dest.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        if entry.is_dir():
            shutil.copytree(entry, dest / entry.name)
        else:
            shutil.copy2(entry, dest / entry.name)


@pytest.fixture
def vncvt_server(tmp_path, request):
    """Spawn vncvt on a free port with scene dumping wired up.

    Yields a ``VncvtHandle``. For backwards compatibility, the handle
    iterates as ``(host, port)`` so ``host, port = vncvt_server`` keeps
    working for tests that don't care about scene dumps.
    """
    handle = _spawn_vncvt((), tmp_path)
    request.node._vncvt_handle = handle  # for failure-dump hook
    try:
        yield handle
    finally:
        _archive_scenes(handle, request.node.nodeid.replace("/", "_").replace("::", "__"))
        _teardown_vncvt(handle)


@pytest.fixture
def vncvt_server_factory(tmp_path, request):
    """Factory variant: lets a test spawn multiple vncvt processes
    with custom args (e.g. ``--password`` for the VNC auth tests).

    Yields a callable returning ``(host, port, proc)``. The returned
    tuple omits the scene handle for backwards compat with existing
    auth tests, but the underlying VncvtHandle is tracked so teardown
    still archives any scene dumps that got written.
    """
    handles: list[VncvtHandle] = []

    def spawn(*extra_args: str) -> tuple[str, int, subprocess.Popen]:
        # Each invocation gets its own subdir under tmp_path so socket
        # paths don't collide across calls.
        sub = tmp_path / f"srv-{len(handles)}"
        sub.mkdir(parents=True, exist_ok=True)
        handle = _spawn_vncvt(tuple(extra_args), sub)
        handles.append(handle)
        return (handle.host, handle.port, handle.proc)

    yield spawn

    for idx, handle in enumerate(handles):
        nodeid = request.node.nodeid.replace("/", "_").replace("::", "__")
        _archive_scenes(handle, f"{nodeid}__srv{idx}")
        _teardown_vncvt(handle)


async def _wait_for_prompt(client, timeout: float = 5.0) -> None:
    """Poll screenshots until the framebuffer shows a rendered prompt.

    The subprocess we spawn needs a moment to start bash and for bash
    to print its first prompt. Tests that assume row 0 has text in it
    (selection, paste, cursor refresh) would otherwise race against
    that warm-up. We loop ``screenshot`` until enough amber pixels
    appear past the cursor column to rule out a plain cursor block.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        rgba = await client.screenshot()
        # Scan row 0 past the cursor cell (first 15px is the cursor
        # block in a fresh screen) for amber text pixels.
        # DEFAULT_BG.red is 26; the cursor/text amber has red > 150.
        top_row = rgba[5:24, 20:400, 0]
        if (top_row > 150).sum() > 100:
            return
        await asyncio.sleep(0.1)
    raise RuntimeError("bash prompt did not render within the timeout")


@pytest_asyncio.fixture
async def vnc(vncvt_server, request):
    """Connected asyncvnc Client against the spawned vncvt, with the
    initial bash prompt already rendered. Also attached to the node
    so the failure hook can dump a final ``test_failed`` scene.
    """
    host, port = vncvt_server
    async with asyncvnc.connect(host, port) as client:
        await _wait_for_prompt(client)
        request.node._vncvt_client = client
        yield client


@pytest_asyncio.fixture
async def scene(vnc, vncvt_server, request):
    """Scene-capture helper bound to the current test's server + client.

    Call as ``await scene("checkpoint_name")`` at any meaningful point.
    Writes a 5-file bundle under the per-test scene dir; directories
    are copied into ``tests/scene-dumps/`` by the server fixture's
    teardown so CI can upload them.
    """
    recorder = SceneRecorder(
        vnc=vnc,
        control_socket=vncvt_server.control_socket,
        scene_root=vncvt_server.scene_dir,
    )
    request.node._vncvt_scene_recorder = recorder
    yield recorder


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """On test failure, try to capture a post-mortem ``test_failed``
    scene with whatever server + client the test had hold of.

    Swallows any secondary errors — a failing test should not have its
    traceback clobbered by a scene-dump hiccup.
    """
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or not report.failed:
        return

    handle = getattr(item, "_vncvt_handle", None)
    client = getattr(item, "_vncvt_client", None)
    if handle is None:
        return

    try:
        scene_dir = asyncio.get_event_loop().run_until_complete(
            _dump_failure_scene(handle, client)
        )
        if scene_dir is not None:
            item.add_report_section(
                "teardown",
                "scene-dump",
                f"Post-mortem scene written to {scene_dir}",
            )
    except Exception as e:  # pragma: no cover — best effort
        item.add_report_section(
            "teardown",
            "scene-dump",
            f"Post-mortem scene dump failed: {e!r}",
        )


async def _dump_failure_scene(handle, client) -> Path | None:
    """Trigger a final server-side DUMP + optional client screenshot
    on test failure. Returns the scene dir or None."""
    try:
        scene_dir = await trigger_server_dump(
            handle.control_socket, "test_failed", timeout=3.0
        )
    except Exception:
        return None
    if client is not None:
        try:
            await capture_client_screenshot(client, scene_dir)
        except Exception:
            pass
    return scene_dir


def pytest_addoption(parser):
    parser.addoption(
        "--update-baselines",
        action="store_true",
        default=False,
        help="Overwrite perceptual-hash baselines with the current values.",
    )


@pytest.fixture
def update_baselines(request) -> bool:
    return bool(request.config.getoption("--update-baselines"))


def pytest_sessionstart(session):
    """Clear any stale scene-dumps from a previous run."""
    if _SCENE_ARCHIVE_ROOT.exists():
        shutil.rmtree(_SCENE_ARCHIVE_ROOT)
    _SCENE_ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
