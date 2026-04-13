"""Test-side helper for capturing scene bundles.

A *scene* is a named checkpoint during a test that snapshots the full
rendering pipeline across five artifacts (see ``vncvt/scene_dump.py``
for the four server-side ones). This module provides the glue:

- Talks to the server's Unix-domain-socket control listener to
  trigger the server-side dump (`DUMP <name>\\n` -> `OK <dir>\\n`).
- Captures the client-side framebuffer via asyncvnc and writes it
  next to the server files as ``scene.client.png``.
- Returns the scene directory path so tests can make assertions
  about the files it produced.

Usage inside a test:

    async def test_something(vnc, scene):
        ...
        await scene("before_action")
        vnc.keyboard.press("Return")
        await scene("after_action")
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import asyncvnc
from PIL import Image


async def trigger_server_dump(
    socket_path: Path,
    scene_name: str,
    timeout: float = 3.0,
) -> Path:
    """Send a DUMP command and wait for the server's OK/ERR response.

    Returns the scene directory path the server reports. Raises
    ``RuntimeError`` on protocol errors or timeout.
    """
    async def _do() -> Path:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
        try:
            writer.write(f"DUMP {scene_name}\n".encode("ascii"))
            await writer.drain()
            line = await reader.readline()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        reply = line.decode("ascii", errors="replace").strip()
        if reply.startswith("OK "):
            return Path(reply[3:])
        raise RuntimeError(f"scene dump failed: {reply!r}")

    return await asyncio.wait_for(_do(), timeout=timeout)


async def capture_client_screenshot(
    vnc: asyncvnc.Client,
    scene_dir: Path,
) -> Path:
    """Take an asyncvnc screenshot and save it as scene.client.png."""
    rgba = await vnc.screenshot()
    img = Image.fromarray(rgba).convert("RGB")
    out = scene_dir / "scene.client.png"
    img.save(out)
    return out


class SceneRecorder:
    """Captures scenes over the life of a single test.

    Tracks the names already used so teardown can detect the implicit
    ``test_failed`` post-mortem scene without colliding.
    """

    def __init__(
        self,
        vnc: asyncvnc.Client,
        control_socket: Path,
        scene_root: Path,
    ) -> None:
        self.vnc = vnc
        self.control_socket = Path(control_socket)
        self.scene_root = Path(scene_root)
        self.captured: list[Path] = []

    async def __call__(self, name: str) -> Path:
        """Capture a named scene: trigger server dump + client shot."""
        scene_dir = await trigger_server_dump(self.control_socket, name)
        # The server writes under its --scene-dump-dir which the test
        # fixture configured to be scene_root, so this should be a
        # subdirectory of scene_root.
        await capture_client_screenshot(self.vnc, scene_dir)
        self.captured.append(scene_dir)
        return scene_dir
