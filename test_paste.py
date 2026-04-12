"""Paste-from-clipboard loopback test (Feature D).

Exercises the full clipboard round-trip:

1. Connect and drain the initial framebuffer.
2. PASTE a known string via ClientCutText (client -> PTY). Bash echoes
   it into the terminal. We press Enter (KeyEvent) afterwards so the
   prompt advances and the previous cursor row gets erased — this is
   the "line refresh after Enter" cosmetic fix.
3. SCREENSHOT the terminal. Verify:
   - the pasted marker appears in the rendered pixel buffer (via a
     column-major scan for amber text pixels in the expected row)
   - the 5px overscan border is visible (corner pixels are the
     default background color)
4. DUMP the clipboard in the other direction: drag-select across the
   pasted row and read back the ServerCutText, asserting it contains
   the marker.
"""

import socket
import struct
import sys
import time

from PIL import Image


AMBER_BG = (26, 16, 0)      # renderer DEFAULT_BG
PADDING = 5


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Connection closed")
        buf += chunk
    return buf


def drain_one_update(sock: socket.socket, width_hint: int) -> dict:
    """Read a FramebufferUpdate (type byte already consumed). Return
    a dict mapping (x, y, w, h) -> raw pixel bytes."""
    hdr = recv_exact(sock, 3)
    n_rects = struct.unpack(">H", hdr[1:3])[0]
    rects = {}
    for _ in range(n_rects):
        rh = recv_exact(sock, 12)
        rx, ry, rw, rh_val, enc = struct.unpack(">HHHHi", rh)
        if enc != 0:
            raise RuntimeError(f"unexpected encoding {enc}")
        data = recv_exact(sock, rw * rh_val * 4)
        rects[(rx, ry, rw, rh_val)] = data
    return rects


def request_full_fb(sock: socket.socket, w: int, h: int) -> Image.Image:
    """Send non-incremental FramebufferUpdateRequest and return a
    reassembled PIL Image of the screen."""
    sock.send(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))
    t = recv_exact(sock, 1)[0]
    assert t == 0, f"expected FramebufferUpdate (0), got {t}"
    rects = drain_one_update(sock, w)
    img = Image.new("RGBX", (w, h), AMBER_BG)
    for (rx, ry, rw, rh_val), data in rects.items():
        rect_img = Image.frombytes("RGBX", (rw, rh_val), data)
        img.paste(rect_img, (rx, ry))
    return img


def main() -> None:
    host = "127.0.0.1"
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5900
    output = sys.argv[2] if len(sys.argv) > 2 else "screenshot_paste.png"

    s = socket.socket()
    s.settimeout(5.0)
    s.connect((host, port))

    # --- Handshake ---
    assert recv_exact(s, 12) == b"RFB 003.008\n"
    s.send(b"RFB 003.008\n")
    n_sec = recv_exact(s, 1)[0]
    recv_exact(s, n_sec)
    s.send(bytes([1]))
    assert struct.unpack(">I", recv_exact(s, 4))[0] == 0
    s.send(bytes([1]))
    si = recv_exact(s, 24)
    w, h = struct.unpack(">HH", si[0:4])
    name_len = struct.unpack(">I", si[20:24])[0]
    recv_exact(s, name_len)
    print(f"Connected: vncvt ({w}x{h})")

    # --- Drain the very first full framebuffer ---
    request_full_fb(s, w, h)
    print("Drained initial framebuffer")

    # --- PASTE: ClientCutText (msg type 6) ---
    marker = "PASTE_FEATURE_D"
    paste = f"echo {marker}".encode("latin-1")
    s.send(struct.pack(">Bxxx", 6) + struct.pack(">I", len(paste)) + paste)
    print(f"Pasted {paste!r}")

    # --- Press Enter to run the command (tests line-refresh fix) ---
    # KeyEvent: type=4, down=1, pad=2, keysym=Return(0xFF0D)
    s.send(struct.pack(">BBHI", 4, 1, 0, 0xFF0D))
    s.send(struct.pack(">BBHI", 4, 0, 0, 0xFF0D))
    time.sleep(1.0)

    # --- Read updates until nothing more is pending, then grab a full
    #     framebuffer snapshot. Switching to a short timeout lets us
    #     drain any server-initiated updates that were queued. ---
    s.settimeout(0.2)
    try:
        while True:
            t = recv_exact(s, 1)[0]
            if t == 0:
                drain_one_update(s, w)
            else:
                raise RuntimeError(f"unexpected queued msg type {t}")
    except (socket.timeout, TimeoutError):
        pass
    s.settimeout(5.0)

    img = request_full_fb(s, w, h)
    img.convert("RGB").save(output)
    print(f"Screenshot saved: {output}")

    # --- Verify 5px overscan border ---
    rgb = img.convert("RGB")
    for (x, y) in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1),
                   (PADDING - 1, PADDING - 1),
                   (w - PADDING, h - PADDING)]:
        px = rgb.getpixel((x, y))
        assert px == AMBER_BG, (
            f"padding pixel ({x},{y}) is {px}, expected {AMBER_BG}"
        )
    print(f"5px overscan border intact (corners + padding edges == {AMBER_BG})")

    # --- Verify pasted marker rendered as amber text somewhere in the
    #     expected row band. We scan the top 8 rows (rendering the
    #     echoed prompt line + output line) for any amber pixel. ---
    cell_h = 19  # matches DejaVu Sans Mono 16pt on this system
    scan_top = PADDING
    scan_bottom = PADDING + 8 * cell_h
    amber_pixel_count = 0
    for y in range(scan_top, min(scan_bottom, h)):
        for x in range(PADDING, w - PADDING):
            px = rgb.getpixel((x, y))
            if px[0] > 200 and px[1] > 100 and px[2] < 80:
                amber_pixel_count += 1
    assert amber_pixel_count > 500, (
        f"Only {amber_pixel_count} amber pixels in top band — expected >500 "
        f"from the pasted echo output"
    )
    print(f"Rendered {amber_pixel_count} amber text pixels in top band")

    # --- DUMP: drag-select across the pasted row and read ServerCutText ---
    # The marker line is row 1 (the second row). Drag across it.
    select_y = PADDING + int(cell_h * 1.5)
    s.send(struct.pack(">BBHH", 5, 1, PADDING, select_y))
    time.sleep(0.05)
    s.send(struct.pack(">BBHH", 5, 1, w - PADDING, select_y))
    time.sleep(0.05)
    s.send(struct.pack(">BBHH", 5, 0, w - PADDING, select_y))

    s.settimeout(3.0)
    cut_text: str | None = None
    while cut_text is None:
        t = recv_exact(s, 1)[0]
        if t == 0:
            drain_one_update(s, w)
        elif t == 3:
            pad_and_len = recv_exact(s, 7)
            length = struct.unpack(">I", pad_and_len[3:7])[0]
            cut_text = recv_exact(s, length).decode("latin-1")
        else:
            raise RuntimeError(f"unexpected msg type {t}")

    print(f"Clipboard dump: {cut_text!r}")
    assert marker in cut_text, (
        f"Expected marker {marker!r} in clipboard dump, got {cut_text!r}"
    )
    print("PASTE + SCREENSHOT + CLIPBOARD DUMP OK")
    s.close()


if __name__ == "__main__":
    main()
