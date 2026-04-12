"""Apple-like pixel format test.

Simulates an Apple Screen Sharing client by sending SetPixelFormat with
BGRX layout (red-shift=16, blue-shift=0) and verifying that the returned
pixel bytes, when interpreted correctly as BGRX, still show amber text.

This catches the pixel-format regression without needing a real Mac.
"""

import socket
import struct
import sys
import time

from PIL import Image


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Connection closed")
        buf += chunk
    return buf


def main() -> None:
    host = "127.0.0.1"
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5900
    output = sys.argv[2] if len(sys.argv) > 2 else "screenshot_apple.png"

    s = socket.socket()
    s.settimeout(5.0)
    s.connect((host, port))

    # --- Handshake ---
    ver = recv_exact(s, 12)
    assert ver == b"RFB 003.008\n"
    s.send(b"RFB 003.008\n")

    n_sec = recv_exact(s, 1)[0]
    recv_exact(s, n_sec)
    s.send(bytes([1]))  # None auth
    result = struct.unpack(">I", recv_exact(s, 4))[0]
    assert result == 0

    s.send(bytes([1]))  # ClientInit shared
    server_init = recv_exact(s, 24)
    w, h = struct.unpack(">HH", server_init[0:4])
    name_len = struct.unpack(">I", server_init[20:24])[0]
    recv_exact(s, name_len)
    print(f"Connected: vncvt ({w}x{h})")

    # --- Immediately send SetPixelFormat with BGRX layout ---
    # RFB msg type 0 + 3 pad + 16-byte PixelFormat
    # bpp=32, depth=24, be=0, tc=1, maxes=255,
    # red-shift=16, green-shift=8, blue-shift=0 -> BGRX memory layout
    pf = struct.pack(
        ">BBBBHHHBBBxxx",
        32, 24, 0, 1, 255, 255, 255, 16, 8, 0,
    )
    s.send(bytes([0, 0, 0, 0]) + pf)
    print("Sent SetPixelFormat: BGRX")

    # --- Request full framebuffer ---
    s.send(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))
    time.sleep(0.5)

    hdr = recv_exact(s, 4)
    n_rects = struct.unpack(">H", hdr[2:4])[0]
    print(f"Update: {n_rects} rect(s)")

    rects = []
    for _ in range(n_rects):
        rh = recv_exact(s, 12)
        rx, ry, rw, rh_val, enc = struct.unpack(">HHHHi", rh)
        data = recv_exact(s, rw * rh_val * 4)
        rects.append((rx, ry, rw, rh_val, data))

    # --- Reassemble, interpreting returned bytes as BGRX ---
    img = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    for rx, ry, rw, rh_val, data in rects:
        # Pillow's "RGBX" raw reader gives RGBA; if server honored our
        # BGRX request, we need to read as BGRX.
        rect_img = Image.frombytes("RGB", (rw, rh_val), data, "raw", "BGRX")
        img.paste(rect_img, (rx, ry))

    img.convert("RGB").save(output)
    print(f"Screenshot saved: {output}")

    # --- Scan the top row band for the brightest non-background pixel ---
    # The prompt "root@..." is in row 0. Find a pixel from rendered text.
    best = (0, 0, 0)
    for y in range(2, 18):
        for x in range(0, min(300, w)):
            px = img.getpixel((x, y))
            r, g, b = px[0], px[1], px[2]
            if r + g + b > best[0] + best[1] + best[2]:
                best = (r, g, b)
    r, g, b = best
    print(f"Brightest text pixel in prompt row: R={r}, G={g}, B={b}")

    # Amber check: red dominant, blue very low.
    # If the server had sent RGBX bytes but declared BGRX, we'd read it
    # back as BGRX and see swapped channels (blue dominant).
    assert r > 100, f"Expected red-dominant amber, got R={r}"
    assert r > b + 50, f"Expected red >> blue (amber); got R={r} B={b}"
    print("BGRX round-trip OK — amber colors preserved")

    s.close()


if __name__ == "__main__":
    main()
