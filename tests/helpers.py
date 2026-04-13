"""Workarounds for AsyncVNC quirks and small RFB helpers.

AsyncVNC 1.3.0 has two protocol-level bugs that bite our tests:

1. ``Clipboard.write`` sends only 1 padding byte after the
   ClientCutText message type (b'\\x06\\x00' + length + text), but
   RFC 6143 §7.5.6 specifies 3 padding bytes.

2. The ``UpdateType`` enum maps msg type 2 to CLIPBOARD and msg type
   3 to BELL, but RFC 6143 §7.6 specifies msg type 2 = Bell and msg
   type 3 = ServerCutText. Any ServerCutText we send gets silently
   dropped by ``Client.read()`` and corrupts the stream.

Both are worked around below. Until they're fixed upstream, tests
should use ``client_cut_text`` to send and ``read_one_message`` to
receive server-to-client messages instead of ``vnc.read()``.
"""

from __future__ import annotations

import struct

import asyncvnc


def client_cut_text(vnc: asyncvnc.Client, text: str) -> None:
    """Send a well-formed RFB ClientCutText (msg type 6) to the server.

    Message layout per RFC 6143 §7.5.6:
        u8  message-type = 6
        u8  padding[3]
        u32 length        (big-endian)
        u8  text[length]
    """
    data = text.encode("latin-1", errors="replace")
    header = b"\x06\x00\x00\x00" + len(data).to_bytes(4, "big")
    vnc.writer.write(header + data)


async def read_one_message(vnc: asyncvnc.Client) -> tuple[int, bytes]:
    """Read one server-to-client RFB message from the underlying reader.

    Returns (msg_type, payload_bytes) where payload is raw (does not
    include the msg_type byte). Handles the three types that vncvt
    actually emits:

    - 0  FramebufferUpdate: consumes pad + rect-count + each rect's
                            header and pixel data; payload is the raw
                            rect blob.
    - 2  Bell: no payload.
    - 3  ServerCutText: consumes 3 pad + u32 length + text; returns
                        the text bytes as payload.

    Used in place of ``asyncvnc.Client.read`` in tests that care about
    ServerCutText — see the docstring at the top of this file for why.
    """
    msg_type = (await vnc.reader.readexactly(1))[0]

    if msg_type == 0:  # FramebufferUpdate
        pad_and_count = await vnc.reader.readexactly(3)
        n_rects = int.from_bytes(pad_and_count[1:3], "big")
        buf = bytearray(pad_and_count)
        for _ in range(n_rects):
            rh = await vnc.reader.readexactly(12)
            x, y, w, h, enc = struct.unpack(">HHHHi", rh)
            buf += rh
            if enc == 0:
                buf += await vnc.reader.readexactly(w * h * 4)
            elif enc == 6:  # zlib
                length = int.from_bytes(
                    await vnc.reader.readexactly(4), "big"
                )
                buf += await vnc.reader.readexactly(length)
            elif enc == -308:  # ExtendedDesktopSize
                buf += await vnc.reader.readexactly(20)
            else:
                raise RuntimeError(f"unsupported rect encoding {enc}")
        return (0, bytes(buf))

    if msg_type == 2:  # Bell
        return (2, b"")

    if msg_type == 3:  # ServerCutText
        pad_and_len = await vnc.reader.readexactly(7)
        length = int.from_bytes(pad_and_len[3:7], "big")
        text = await vnc.reader.readexactly(length)
        return (3, text)

    raise RuntimeError(f"unknown server-to-client msg type {msg_type}")
