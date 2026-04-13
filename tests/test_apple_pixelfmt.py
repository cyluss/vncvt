"""Raw-protocol test: client SetPixelFormat honoring (Apple BGRX).

AsyncVNC hides the pixel format negotiation — its Video decoder always
reinterprets bytes into its internal RGBA buffer, so a broken server
would look correct to the client. This test is deliberately low-level:
it sends a SetPixelFormat requesting BGRX (red-shift=16, blue-shift=0,
little-endian), reads back the framebuffer, and asserts that when we
parse those bytes as BGRX we see amber text — i.e. the server did
respect the request and sent B in the low byte.
"""

from __future__ import annotations

import socket
import struct
import time

import pytest
from PIL import Image


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf


def test_bgrx_pixel_format_round_trip(vncvt_server):
    host, port = vncvt_server
    s = socket.create_connection((host, port), timeout=5.0)
    try:
        # RFB handshake.
        assert _recv_exact(s, 12) == b"RFB 003.008\n"
        s.send(b"RFB 003.008\n")
        n_sec = _recv_exact(s, 1)[0]
        _recv_exact(s, n_sec)
        s.send(bytes([1]))  # None auth
        assert struct.unpack(">I", _recv_exact(s, 4))[0] == 0
        s.send(bytes([1]))  # ClientInit shared
        server_init = _recv_exact(s, 24)
        w, h = struct.unpack(">HH", server_init[0:4])
        name_len = struct.unpack(">I", server_init[20:24])[0]
        _recv_exact(s, name_len)

        # Give the bash prompt time to render before we screenshot.
        time.sleep(0.5)

        # Send SetPixelFormat (type 0 + 3 pad + 16 byte PixelFormat)
        # with BGRX layout: red-shift=16, green-shift=8, blue-shift=0.
        pf = struct.pack(
            ">BBBBHHHBBBxxx",
            32, 24, 0, 1, 255, 255, 255, 16, 8, 0,
        )
        s.send(bytes([0, 0, 0, 0]) + pf)

        # Non-incremental framebuffer update request.
        s.send(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))
        time.sleep(0.5)

        # Read the FramebufferUpdate response.
        hdr = _recv_exact(s, 4)
        assert hdr[0] == 0  # FramebufferUpdate
        n_rects = struct.unpack(">H", hdr[2:4])[0]

        img = Image.new("RGB", (w, h), (0, 0, 0))
        for _ in range(n_rects):
            rh = _recv_exact(s, 12)
            rx, ry, rw, rh_val, enc = struct.unpack(">HHHHi", rh)
            assert enc == 0, f"expected Raw encoding, got {enc}"
            data = _recv_exact(s, rw * rh_val * 4)
            # Interpret as BGRX (what we asked for). Pillow's raw reader
            # reads the 4 bytes per pixel and maps them by mode name.
            rect_img = Image.frombytes("RGB", (rw, rh_val), data, "raw", "BGRX")
            img.paste(rect_img, (rx, ry))

        # Scan the prompt row band for the brightest text pixel. If
        # the server honored BGRX, we read amber correctly: R dominant,
        # low B. If it didn't (sent RGBX anyway), R and B would swap
        # and we'd see blue text.
        best = (0, 0, 0)
        for y in range(2, 20):
            for x in range(20, 400):
                px = img.getpixel((x, y))
                if sum(px) > sum(best):
                    best = px
        r, g, b = best
        assert r > 200, f"expected amber (R > 200), got R={r} G={g} B={b}"
        assert r > b + 100, (
            f"expected red much greater than blue (amber), got R={r} B={b} "
            f"— server may not be honoring SetPixelFormat"
        )
    finally:
        s.close()
