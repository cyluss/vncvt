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


def _read_full_framebuffer(
    s: socket.socket, w: int, h: int, mode: str = "BGRX"
) -> Image.Image:
    """Send a non-incremental FramebufferUpdateRequest and decode the
    full Raw-encoded response into a Pillow image, treating each pixel
    as ``mode`` (the wire format we asked for via SetPixelFormat).
    """
    s.send(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))
    hdr = _recv_exact(s, 4)
    assert hdr[0] == 0  # FramebufferUpdate
    n_rects = struct.unpack(">H", hdr[2:4])[0]
    img = Image.new("RGB", (w, h), (0, 0, 0))
    for _ in range(n_rects):
        rh = _recv_exact(s, 12)
        rx, ry, rw, rh_val, enc = struct.unpack(">HHHHi", rh)
        assert enc == 0, f"expected Raw encoding, got {enc}"
        data = _recv_exact(s, rw * rh_val * 4)
        # Pillow's raw reader maps the 4 bytes per pixel by mode name.
        rect_img = Image.frombytes("RGB", (rw, rh_val), data, "raw", mode)
        img.paste(rect_img, (rx, ry))
    return img


def _brightest_red_in_prompt_band(img: Image.Image) -> int:
    """Return the maximum red-channel value in the top-of-frame band
    where bash's first prompt would be drawn. Used as a "is the prompt
    rendered yet?" signal — amber text has R=255 in the high byte;
    the background colour is R=26.
    """
    best = 0
    band_h = min(20, img.height)
    for y in range(band_h):
        for x in range(min(400, img.width)):
            r = img.getpixel((x, y))[0]
            if r > best:
                best = r
    return best


def test_bgrx_pixel_format_round_trip(vncvt_server):
    host, port = vncvt_server
    s = socket.create_connection((host, port), timeout=5.0)
    try:
        # RFB handshake with VNC auth (default password "vncvt").
        from vncvt.server import _vnc_encrypt
        assert _recv_exact(s, 12) == b"RFB 003.008\n"
        s.send(b"RFB 003.008\n")
        n_sec = _recv_exact(s, 1)[0]
        _recv_exact(s, n_sec)
        s.send(bytes([2]))  # VNC auth
        challenge = _recv_exact(s, 16)
        s.send(_vnc_encrypt(challenge, "vncvt"))
        assert struct.unpack(">I", _recv_exact(s, 4))[0] == 0
        s.send(bytes([1]))  # ClientInit shared
        server_init = _recv_exact(s, 24)
        w, h = struct.unpack(">HH", server_init[0:4])
        name_len = struct.unpack(">I", server_init[20:24])[0]
        _recv_exact(s, name_len)

        # Send SetPixelFormat (type 0 + 3 pad + 16 byte PixelFormat)
        # with BGRX layout: red-shift=16, green-shift=8, blue-shift=0.
        pf = struct.pack(
            ">BBBBHHHBBBxxx",
            32, 24, 0, 1, 255, 255, 255, 16, 8, 0,
        )
        s.send(bytes([0, 0, 0, 0]) + pf)

        # Poll the framebuffer until the bash prompt has actually been
        # rendered. The previous version of this test used a fixed
        # 0.5s sleep, which was racy when the spawned shell is a login
        # shell (it has to source /etc/profile + ~/.profile before
        # printing its first prompt). Poll up to 5s in 0.1s steps.
        deadline = time.monotonic() + 5.0
        img = None
        while time.monotonic() < deadline:
            img = _read_full_framebuffer(s, w, h, mode="BGRX")
            if _brightest_red_in_prompt_band(img) > 200:
                break
            time.sleep(0.1)
        else:
            assert img is not None
            raise AssertionError(
                "bash prompt did not render within 5s "
                f"(brightest R in prompt band: "
                f"{_brightest_red_in_prompt_band(img)})"
            )

        # The poll loop above already proved the prompt is rendered
        # by checking the red channel. Now check the FULL pixel of
        # the brightest text cell to verify R dominates B — i.e. the
        # server actually honored our BGRX request. If it had silently
        # sent RGBX instead, we'd be reinterpreting the bytes as BGRX
        # and would see blue (high B, low R) instead of amber.
        best = (0, 0, 0)
        for y in range(min(20, img.height)):
            for x in range(min(400, img.width)):
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
