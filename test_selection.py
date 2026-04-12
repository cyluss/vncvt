"""Selection + ServerCutText loopback test.

1. Handshake.
2. Drain the initial framebuffer.
3. Send PointerEvent down → drag → up across row 0 of the terminal.
4. Read messages until a ServerCutText (msg type 3) arrives.
5. Assert the cut text contains expected prompt text.
"""

import socket
import struct
import sys
import time


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Connection closed")
        buf += chunk
    return buf


def drain_framebuffer_update(sock: socket.socket) -> None:
    """Read the remainder of a FramebufferUpdate after consuming its type byte."""
    hdr = recv_exact(sock, 3)  # 1 pad + 2 rect-count
    n_rects = struct.unpack(">H", hdr[1:3])[0]
    for _ in range(n_rects):
        rh = recv_exact(sock, 12)
        _, _, rw, rh_val, enc = struct.unpack(">HHHHi", rh)
        if enc != 0:
            raise RuntimeError(f"Unexpected encoding {enc}")
        recv_exact(sock, rw * rh_val * 4)


def main() -> None:
    host = "127.0.0.1"
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5900

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

    # --- Drain initial framebuffer (one rect covering the whole screen) ---
    s.send(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))
    msg_type = recv_exact(s, 1)[0]
    assert msg_type == 0
    drain_framebuffer_update(s)
    print("Drained initial framebuffer")

    # --- Send PointerEvent drag across row 0 ---
    def pointer(button: int, x: int, y: int) -> bytes:
        return struct.pack(">BBHH", 5, button, x, y)

    s.send(pointer(1, 0, 5))         # button down, far left
    time.sleep(0.05)
    s.send(pointer(1, 300, 5))       # drag to ~col 30
    time.sleep(0.05)
    s.send(pointer(0, 300, 5))       # release

    # --- Read messages until we see a ServerCutText (type 3) ---
    s.settimeout(3.0)
    cut_text: str | None = None
    while cut_text is None:
        msg_type = recv_exact(s, 1)[0]
        if msg_type == 0:
            drain_framebuffer_update(s)
        elif msg_type == 3:
            pad_and_len = recv_exact(s, 7)
            length = struct.unpack(">I", pad_and_len[3:7])[0]
            cut_text = recv_exact(s, length).decode("latin-1")
            print(f"ServerCutText: {cut_text!r}")
        else:
            raise RuntimeError(f"Unexpected msg type {msg_type}")

    # The selection covers part of the default bash prompt row, which
    # includes "root@" in nearly any shell. Accept any non-empty text
    # that contains a typical prompt substring.
    assert len(cut_text) > 0, f"Empty selection: {cut_text!r}"
    assert any(m in cut_text for m in ("root@", "@", "#", "$", ":")), (
        f"Selection text doesn't look like a prompt row: {cut_text!r}"
    )
    print("SELECTION + SERVERCUTTEXT OK")
    s.close()


if __name__ == "__main__":
    main()
