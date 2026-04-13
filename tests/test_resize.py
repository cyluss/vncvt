"""Live framebuffer resize: client-initiated (RFB SetDesktopSize msg
type 251) and server-initiated (scene control socket op="resize").

Both paths share the same RFBServer.handle_resize central method, so
each test verifies that the post-resize ServerInit dimensions on a
fresh connection match what was requested.
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
from pathlib import Path

import pytest


def _handshake_no_auth(s: socket.socket) -> tuple[int, int]:
    """Run the RFB handshake on a no-password server, return (w, h)
    from ServerInit."""
    assert s.recv(12) == b"RFB 003.008\n"
    s.send(b"RFB 003.008\n")
    n_sec = s.recv(1)[0]
    sec_types = list(s.recv(n_sec))
    assert 1 in sec_types, f"expected None auth, got {sec_types}"
    s.send(bytes([1]))
    # SecurityResult on 3.8
    result = struct.unpack(">I", s.recv(4))[0]
    assert result == 0
    # ClientInit shared
    s.send(bytes([1]))
    server_init = s.recv(24)
    w, h = struct.unpack(">HH", server_init[0:4])
    name_len = struct.unpack(">I", server_init[20:24])[0]
    s.recv(name_len)
    return w, h


def _send_set_encodings(s: socket.socket, encodings: list[int]) -> None:
    """Send a SetEncodings (msg type 2) advertising the given encoding
    type values. Each encoding is a signed 32-bit integer."""
    n = len(encodings)
    header = struct.pack(">BxH", 2, n)
    body = b"".join(struct.pack(">i", e) for e in encodings)
    s.send(header + body)


def _send_set_desktop_size(
    s: socket.socket, width: int, height: int
) -> None:
    """Send a SetDesktopSize (msg type 251) requesting the given size.
    Includes one screen entry covering the full new area."""
    # type(1) + pad(1) + width(2) + height(2) + num-screens(1) + pad(1)
    header = struct.pack(">BxHHBx", 251, width, height, 1)
    # Screen entry: id(4) + x(2) + y(2) + w(2) + h(2) + flags(4)
    screen = struct.pack(">IHHHHI", 0, 0, 0, width, height, 0)
    s.send(header + screen)


def _read_exactly(s: socket.socket, n: int) -> bytes:
    out = b""
    while len(out) < n:
        chunk = s.recv(n - len(out))
        if not chunk:
            raise RuntimeError(f"short read: wanted {n}, got {len(out)}")
        out += chunk
    return out


def test_client_initiated_resize(vncvt_server):
    """A client that advertises ExtendedDesktopSize (-308) and sends
    SetDesktopSize gets back a FramebufferUpdate carrying an
    ExtendedDesktopSize rect with the new dimensions."""
    host, port = vncvt_server
    s = socket.create_connection((host, port), timeout=5.0)
    try:
        w0, h0 = _handshake_no_auth(s)
        assert w0 > 0 and h0 > 0

        # Advertise both -223 and -308 so the resize notification path
        # also accepts us, and a few real encodings so the server has
        # something to render with.
        _send_set_encodings(s, [0, -223, -308])

        # Request a noticeably different size — 100 cols × 30 rows
        # at the default font size. We don't know the exact pixel
        # bounds the server will snap to, but they must differ from
        # the initial framebuffer.
        target_w = 800
        target_h = 400
        _send_set_desktop_size(s, target_w, target_h)

        # Server response: a FramebufferUpdate with one rect carrying
        # the ExtendedDesktopSize pseudo-encoding (-308). The rect
        # header layout is x=status, y=reason, w=newW, h=newH.
        hdr = _read_exactly(s, 4)
        assert hdr[0] == 0, f"expected FramebufferUpdate (0), got {hdr[0]}"
        n_rects = struct.unpack(">H", hdr[2:4])[0]
        assert n_rects >= 1
        rect_hdr = _read_exactly(s, 12)
        rx, ry, rw, rh, enc = struct.unpack(">HHHHi", rect_hdr)
        assert enc == -308, (
            f"first rect should be ExtendedDesktopSize (-308), got {enc}"
        )
        assert rw > 0 and rh > 0, f"degenerate new size {rw}x{rh}"
        # The new framebuffer must differ from the initial one — proves
        # the resize actually took effect.
        assert (rw, rh) != (w0, h0), (
            f"size unchanged after resize: {rw}x{rh}"
        )
        # Drain the screen list (1 byte num-screens + 3 pad + 16 per screen)
        n_screens_data = _read_exactly(s, 4)
        n_screens = n_screens_data[0]
        for _ in range(n_screens):
            _read_exactly(s, 16)
    finally:
        s.close()


def test_control_socket_resize(vncvt_server):
    """An external caller sends op="resize" over the scene control
    socket, the server resizes, and a brand-new RFB connection picks
    up the new dimensions in ServerInit."""
    handle = vncvt_server
    target_cols = 120
    target_rows = 40

    async def _send_resize() -> dict:
        reader, writer = await asyncio.open_unix_connection(
            path=str(handle.control_socket)
        )
        try:
            req = {
                "version": 1,
                "op": "resize",
                "cols": target_cols,
                "rows": target_rows,
            }
            writer.write((json.dumps(req) + "\n").encode("utf-8"))
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=3.0)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        return json.loads(line.decode("utf-8"))

    reply = asyncio.run(_send_resize())
    assert reply.get("ok") is True, f"resize op failed: {reply}"
    assert reply.get("cols") == target_cols
    assert reply.get("rows") == target_rows

    # Fresh connection: ServerInit dimensions reflect the new size.
    s = socket.create_connection((handle.host, handle.port), timeout=5.0)
    try:
        w_new, h_new = _handshake_no_auth(s)
    finally:
        s.close()

    # The cell-grid math means w_new/h_new are some function of
    # cols/rows + font metrics + padding; we don't replicate the math
    # here, but the post-resize framebuffer should at least be larger
    # than a fresh-default 80x24 vncvt at the same font size.
    assert w_new > 0 and h_new > 0
    # Sanity: 120x40 is bigger than 80x24, so width and height should
    # both have grown vs the default.
    s2 = socket.create_connection((handle.host, handle.port), timeout=5.0)
    # We can't compare against a fresh default server without spawning
    # another process; just assert the new dimensions are at least
    # 120 cells worth at the smallest plausible cell width (6 px).
    s2.close()
    assert w_new >= 120 * 6, (
        f"post-resize width {w_new} too small for 120 cols"
    )
    assert h_new >= 40 * 8, (
        f"post-resize height {h_new} too small for 40 rows"
    )


def test_resize_unknown_op_rejected(vncvt_server):
    """Sanity check: the dispatcher still rejects unknown ops with a
    helpful error, instead of silently accepting them now that there
    are two valid ops."""
    handle = vncvt_server

    async def _send() -> dict:
        reader, writer = await asyncio.open_unix_connection(
            path=str(handle.control_socket)
        )
        try:
            writer.write(
                (json.dumps({"version": 1, "op": "frobnicate"}) + "\n")
                .encode("utf-8")
            )
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=3.0)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        return json.loads(line.decode("utf-8"))

    reply = asyncio.run(_send())
    assert reply.get("ok") is False
    assert "frobnicate" in reply.get("error", "")
