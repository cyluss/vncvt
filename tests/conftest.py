"""Shared pytest fixtures for the vncvt regression suite.

One fixture spawns `vncvt` on a free port and tears it down cleanly.
Another connects an AsyncVNC client to that server.

Tests that need byte-level protocol control talk to the server via
raw sockets and use the same `vncvt_server` fixture for lifecycle.
Tests that want a high-level view (screenshot, type, drag) use
the `vnc` fixture.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import time
from contextlib import asynccontextmanager

import asyncvnc
import pytest
import pytest_asyncio


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


@pytest.fixture
def vncvt_server():
    """Spawn vncvt on a random free port. Yields (host, port)."""
    port = _free_port()
    proc = subprocess.Popen(
        ["uv", "run", "python", "-m", "vncvt", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    try:
        _wait_for_listen("127.0.0.1", port)
    except Exception:
        proc.kill()
        raise
    yield ("127.0.0.1", port)
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


@pytest.fixture
def vncvt_server_factory():
    """Factory variant: lets a test spawn vncvt with custom args
    (e.g. --password for VNC auth tests). Yields a callable that
    returns (host, port, proc_handle). Caller must not kill proc;
    teardown does it automatically.
    """
    procs: list[subprocess.Popen] = []

    def spawn(*extra_args: str) -> tuple[str, int, subprocess.Popen]:
        port = _free_port()
        proc = subprocess.Popen(
            ["uv", "run", "python", "-m", "vncvt",
             "--port", str(port), *extra_args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        procs.append(proc)
        try:
            _wait_for_listen("127.0.0.1", port)
        except Exception:
            proc.kill()
            raise
        return ("127.0.0.1", port, proc)

    yield spawn

    for proc in procs:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


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
async def vnc(vncvt_server):
    """Connected asyncvnc Client against the spawned vncvt, with the
    initial bash prompt already rendered."""
    host, port = vncvt_server
    async with asyncvnc.connect(host, port) as client:
        await _wait_for_prompt(client)
        yield client


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
