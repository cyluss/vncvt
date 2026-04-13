"""Per-scene debugging artifact dumper.

A *scene* is a named checkpoint during a test run at which we snapshot
the server's internal state. Each snapshot produces a bundle of five
files that together span the full rendering pipeline:

    scene.text       "\\n".join(pyte.Screen.display) — visible text grid
    scene.hex.gz     gzipped hex dump of Renderer.image.tobytes("raw",
                     "RGBX"). The raw dump is highly redundant (most
                     pixels are DEFAULT_BG), so gzip usually shrinks
                     ~5 MB to <100 KB. Inspect with ``zless`` or
                     ``zcat scene.hex.gz | less``.
    scene.fb.png     Renderer.image saved as PNG (server ground truth)
    scene.term.json  full pyte state + vncvt selection/cursor extras
    scene.client.png captured by the test on the client side
                     (asyncvnc screenshot on Linux, screencapture crop
                     on macOS) — written by the test, not this module

``SceneDumper`` writes the first four. The client-side PNG is injected
into the same directory by the test helper in ``tests/scenes.py``.

A thin Unix-domain-socket control server is exposed so the dumper can
be triggered from outside the Python process — tests, shell scripts,
and the Mac CI job all talk to it the same way:

    printf 'DUMP my_scene_name\\n' | nc -U /tmp/vncvt-scene.sock

The server responds with ``OK <scene_dir>\\n`` on success or
``ERR <message>\\n`` on failure. See ``serve_control_socket`` below.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .terminal import Terminal
    from .renderer import TerminalRenderer

log = logging.getLogger(__name__)

_HEX_BYTES_PER_LINE = 32


def _hexdump(data: bytes) -> str:
    """Format bytes as an offset-prefixed hex dump, 32 bytes per line."""
    lines: list[str] = []
    for offset in range(0, len(data), _HEX_BYTES_PER_LINE):
        chunk = data[offset : offset + _HEX_BYTES_PER_LINE]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        lines.append(f"{offset:08x}  {hex_part}")
    return "\n".join(lines) + "\n"


def _serialize_char(char: object) -> dict | None:
    """Sparse dict for a single pyte Char, or None if it's a blank cell.

    Only non-default attributes are recorded, so a 80x24 dump of a
    mostly-empty screen stays readable.
    """
    data = getattr(char, "data", "")
    fg = getattr(char, "fg", "default")
    bg = getattr(char, "bg", "default")
    bold = getattr(char, "bold", False)
    italics = getattr(char, "italics", False)
    underscore = getattr(char, "underscore", False)
    strikethrough = getattr(char, "strikethrough", False)
    reverse = getattr(char, "reverse", False)
    blink = getattr(char, "blink", False)

    is_blank = (
        (data in ("", " "))
        and fg == "default"
        and bg == "default"
        and not (bold or italics or underscore or strikethrough or reverse or blink)
    )
    if is_blank:
        return None

    out: dict = {"c": data}
    if fg != "default":
        out["fg"] = fg
    if bg != "default":
        out["bg"] = bg
    if bold:
        out["bold"] = True
    if italics:
        out["italics"] = True
    if underscore:
        out["underscore"] = True
    if strikethrough:
        out["strikethrough"] = True
    if reverse:
        out["reverse"] = True
    if blink:
        out["blink"] = True
    return out


def _serialize_terminal(term: "Terminal") -> dict:
    """Build a JSON-ready dict representing the pyte Screen plus the
    vncvt-level state carried on ``Terminal``."""
    screen = term.screen
    cells: dict[str, dict] = {}
    for row_idx in range(term.rows):
        buffer_row = screen.buffer[row_idx]
        for col_idx in range(term.cols):
            ch = buffer_row[col_idx]
            encoded = _serialize_char(ch)
            if encoded is not None:
                cells[f"{col_idx},{row_idx}"] = encoded

    cursor_info = {
        "x": int(screen.cursor.x),
        "y": int(screen.cursor.y),
        "hidden": bool(getattr(screen.cursor, "hidden", False)),
    }
    attrs = getattr(screen.cursor, "attrs", None)
    if attrs is not None:
        attrs_enc = _serialize_char(attrs)
        if attrs_enc is not None:
            cursor_info["attrs"] = attrs_enc

    margins = getattr(screen, "margins", None)
    margins_info: dict | None = None
    if margins is not None:
        try:
            margins_info = {"top": int(margins.top), "bottom": int(margins.bottom)}
        except Exception:  # pragma: no cover — defensive
            margins_info = None

    modes_list: list[int] = []
    modes_attr = getattr(screen, "mode", None)
    if modes_attr is not None:
        try:
            modes_list = sorted(int(m) for m in modes_attr)
        except Exception:  # pragma: no cover
            modes_list = []

    tabstops_list: list[int] = []
    tabstops_attr = getattr(screen, "tabstops", None)
    if tabstops_attr is not None:
        try:
            tabstops_list = sorted(int(t) for t in tabstops_attr)
        except Exception:  # pragma: no cover
            tabstops_list = []

    return {
        "dimensions": {"cols": term.cols, "rows": term.rows},
        "cursor": cursor_info,
        "last_cursor": list(term._last_cursor),
        "selection_anchor": (
            list(term.selection_anchor) if term.selection_anchor is not None else None
        ),
        "selection_head": (
            list(term.selection_head) if term.selection_head is not None else None
        ),
        "margins": margins_info,
        "modes": modes_list,
        "tabstops": tabstops_list,
        "display": list(screen.display),
        "cells": cells,
    }


class SceneDumper:
    """Writes a per-scene artifact bundle to a directory on demand."""

    def __init__(
        self,
        terminal: "Terminal",
        renderer: "TerminalRenderer",
        out_dir: Path,
    ) -> None:
        self.terminal = terminal
        self.renderer = renderer
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def dump(self, name: str) -> Path:
        """Write the four server-side artifacts under ``out_dir/<name>/``.

        Returns the scene directory path so callers can find the files.
        Raises ``ValueError`` if the scene name contains path separators.
        """
        if not name or "/" in name or "\\" in name or ".." in name:
            raise ValueError(f"invalid scene name: {name!r}")

        scene_dir = self.out_dir / name
        scene_dir.mkdir(parents=True, exist_ok=True)

        # 1. text — pyte's visible display as a joined string
        text = "\n".join(self.terminal.screen.display)
        (scene_dir / "scene.text").write_text(text + "\n", encoding="utf-8")

        # 2. hex.gz — rendered framebuffer RGBX bytes as an offset+hex
        # dump, gzipped because the raw form is ~5 MB of mostly zeros
        # and adds up fast over 10+ scenes.
        raw = self.renderer.image.tobytes("raw", "RGBX")
        hex_text = _hexdump(raw).encode("ascii")
        with gzip.open(scene_dir / "scene.hex.gz", "wb", compresslevel=6) as f:
            f.write(hex_text)

        # 3. fb.png — renderer's Pillow image as a PNG
        # Pillow's "RGBX" mode saves via "RGB" under the hood; convert
        # explicitly so PNG metadata is clean.
        self.renderer.image.convert("RGB").save(scene_dir / "scene.fb.png")

        # 4. term.json — pyte Screen state + vncvt extras, sparse-encoded
        payload = _serialize_terminal(self.terminal)
        (scene_dir / "scene.term.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        log.info("scene.dump: wrote %s", scene_dir)
        return scene_dir


async def serve_control_socket(
    dumper: SceneDumper,
    socket_path: Path,
) -> asyncio.base_events.Server:
    """Start an asyncio Unix-domain-socket server that triggers dumps.

    Protocol is line-oriented ASCII:

        client -> server:   ``DUMP <name>\\n``
        server -> client:   ``OK <scene_dir>\\n``    on success
                            ``ERR <reason>\\n``     on failure

    Returns the started ``asyncio.Server``. Caller is responsible for
    serving it (via ``asyncio.create_task(server.serve_forever())``)
    and for unlinking the socket file on shutdown.
    """
    socket_path = Path(socket_path)
    if socket_path.exists():
        socket_path.unlink()

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            parts = line.decode("ascii", errors="replace").strip().split(None, 1)
            if len(parts) != 2 or parts[0] != "DUMP":
                writer.write(b"ERR expected 'DUMP <name>'\n")
                await writer.drain()
                return
            name = parts[1]
            try:
                scene_dir = dumper.dump(name)
            except Exception as e:
                writer.write(f"ERR {e}\n".encode("ascii"))
                await writer.drain()
                return
            writer.write(f"OK {scene_dir}\n".encode("ascii"))
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_unix_server(handle, path=str(socket_path))
    # Tighten socket permissions — we don't want another user on the
    # CI runner injecting scene dumps.
    try:
        os.chmod(socket_path, 0o600)
    except OSError:  # pragma: no cover — best effort
        pass
    log.info("scene control socket listening on %s", socket_path)
    return server
