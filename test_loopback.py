"""Loopback test: connect to vncvt, type a command, screenshot, exit."""

import socket
import struct
import time
import sys
from PIL import Image


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Receive exactly n bytes."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Connection closed")
        buf += chunk
    return buf


def main():
    host = "127.0.0.1"
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5900
    output = sys.argv[2] if len(sys.argv) > 2 else "screenshot.png"

    s = socket.socket()
    s.settimeout(5.0)
    s.connect((host, port))

    # --- RFB Handshake ---
    ver = recv_exact(s, 12)
    assert ver == b"RFB 003.008\n", f"Bad version: {ver!r}"
    s.send(b"RFB 003.008\n")

    n_sec = recv_exact(s, 1)[0]
    sec_types = recv_exact(s, n_sec)
    s.send(bytes([1]))  # None auth

    result = struct.unpack(">I", recv_exact(s, 4))[0]
    assert result == 0, f"Auth failed: {result}"

    s.send(bytes([1]))  # ClientInit shared=True

    # ServerInit
    server_init = recv_exact(s, 24)
    w, h = struct.unpack(">HH", server_init[0:4])
    name_len = struct.unpack(">I", server_init[20:24])[0]
    name = recv_exact(s, name_len).decode()
    print(f"Connected: {name} ({w}x{h})")

    # --- Request initial framebuffer ---
    s.send(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))
    time.sleep(0.5)

    def read_update() -> dict[tuple[int, int, int, int], bytes]:
        """Read a FramebufferUpdate and return rectangles."""
        hdr = recv_exact(s, 4)
        _, n_rects = struct.unpack(">BxH", hdr)
        rects = {}
        for _ in range(n_rects):
            rect_hdr = recv_exact(s, 12)
            rx, ry, rw, rh, enc = struct.unpack(">HHHHi", rect_hdr)
            pixel_data = recv_exact(s, rw * rh * 4)
            rects[(rx, ry, rw, rh)] = pixel_data
        return rects

    rects = read_update()
    print(f"Initial update: {len(rects)} rect(s)")

    # --- Type a command ---
    command = "echo 'Hello from vncvt!' && ls -la\n"
    print(f"Typing: {command.strip()}")
    for ch in command:
        keysym = ord(ch)
        if ch == "\n":
            keysym = 0xFF0D
        # Key down
        s.send(struct.pack(">BBHI", 4, 1, 0, keysym))
        # Key up
        s.send(struct.pack(">BBHI", 4, 0, 0, keysym))
        time.sleep(0.01)

    # Wait for output to render
    time.sleep(1.0)

    # --- Request incremental update to get latest screen ---
    s.send(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))  # non-incremental for full screen
    time.sleep(0.5)
    rects = read_update()
    print(f"Final update: {len(rects)} rect(s)")

    # --- Compose screenshot ---
    img = Image.new("RGBX", (w, h), (0, 0, 0, 0))
    for (rx, ry, rw, rh), data in rects.items():
        rect_img = Image.frombytes("RGBX", (rw, rh), data)
        img.paste(rect_img, (rx, ry))

    img.convert("RGB").save(output)
    print(f"Screenshot saved: {output}")

    s.close()
    print("Done")


if __name__ == "__main__":
    main()
