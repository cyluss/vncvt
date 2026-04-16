"""RFB (VNC) protocol client handler.

Extracted from server.py — handles a single VNC client connection,
including RFB handshake, authentication, and the client message loop.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import secrets
import struct
import zlib
from typing import TYPE_CHECKING

from .keysym import keysym_to_bytes

if TYPE_CHECKING:
    from .server import RFBServer

log = logging.getLogger(__name__)

# Opt-in verbose RFB protocol tracing. Set VNCVT_LOG_RFB=1 in the
# environment to log every handshake step and every client message
# received. CI uses this to diagnose client-compatibility hangs.
_RFB_TRACE = os.environ.get("VNCVT_LOG_RFB") == "1"


def _trace(fmt: str, *args: object) -> None:
    if _RFB_TRACE:
        log.info("RFB: " + fmt, *args)


# Pillow raw modes supported for 32bpp true-colour output.
_SUPPORTED_RAW_MODES = frozenset({
    "RGBX", "BGRX", "XRGB", "XBGR",
    "RGBA", "BGRA", "ARGB", "ABGR",
})


def _choose_raw_mode(pf: dict) -> str:
    """Pick a Pillow raw mode string matching the client's pixel format.

    Given a PixelFormat dict with keys bpp, true_colour, *_max, *_shift,
    big_endian, derive the matching Pillow raw output mode so that
    `image.tobytes('raw', mode)` produces the exact byte layout the
    client expects.
    """
    if (
        pf["bpp"] != 32
        or not pf["true_colour"]
        or pf["red_max"] != 255
        or pf["green_max"] != 255
        or pf["blue_max"] != 255
    ):
        log.warning("Unsupported pixel format %s; falling back to RGBA", pf)
        return "RGBA"

    byte_pos = {
        "R": pf["red_shift"] // 8,
        "G": pf["green_shift"] // 8,
        "B": pf["blue_shift"] // 8,
    }
    used = set(byte_pos.values())
    if len(used) != 3 or not used.issubset({0, 1, 2, 3}):
        log.warning("Unusual shifts in %s; falling back to RGBA", pf)
        return "RGBA"

    x_pos = ({0, 1, 2, 3} - used).pop()
    layout = [None, None, None, None]
    for name, pos in byte_pos.items():
        layout[pos] = name
    layout[x_pos] = "X"
    mode = "".join(layout)

    # Big-endian wire order is MSB..LSB; reverse the in-memory layout.
    if pf["big_endian"]:
        mode = mode[::-1]

    if mode not in _SUPPORTED_RAW_MODES:
        log.warning("Derived mode %s not supported by Pillow; using RGBA", mode)
        return "RGBA"
    return mode


def _vnc_encrypt(challenge: bytes, password: str) -> bytes:
    """Encrypt a VNC auth challenge with the given password (single-DES ECB).

    Per RFC 6143 §7.2.2 and historical VNC convention: the password is
    truncated/padded to 8 bytes, and each byte has its bits reversed
    before being used as the DES key.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, modes
    try:
        from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    except ImportError:  # pragma: no cover — older cryptography versions
        from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES

    key = password.encode("latin-1", errors="replace")[:8].ljust(8, b"\x00")
    # Bit-reverse each byte (VNC historical quirk).
    key = bytes(int(f"{b:08b}"[::-1], 2) for b in key)
    # `cryptography` exposes single-DES via TripleDES with K1=K2=K3.
    cipher = Cipher(TripleDES(key + key + key), modes.ECB())
    enc = cipher.encryptor()
    return enc.update(challenge) + enc.finalize()


def _fmt_hex(data: bytes, max_bytes: int = 64) -> str:
    """Format bytes as space-separated hex with a size cap."""
    if len(data) <= max_bytes:
        return f"{len(data):5d}B  {data.hex(' ')}"
    head = data[:max_bytes].hex(" ")
    return f"{len(data):5d}B  {head} ... (+{len(data) - max_bytes})"


class _LoggingReader:
    """Wraps asyncio.StreamReader and logs each read as hex."""

    def __init__(self, reader: asyncio.StreamReader, prefix: str):
        self._reader = reader
        self._prefix = prefix

    async def readexactly(self, n: int) -> bytes:
        data = await self._reader.readexactly(n)
        log.info("%s %s", self._prefix, _fmt_hex(data))
        return data


class _LoggingWriter:
    """Wraps asyncio.StreamWriter and logs each write as hex."""

    def __init__(self, writer: asyncio.StreamWriter, prefix: str):
        self._writer = writer
        self._prefix = prefix

    def write(self, data: bytes) -> None:
        log.info("%s %s", self._prefix, _fmt_hex(data))
        self._writer.write(data)

    async def drain(self) -> None:
        await self._writer.drain()

    def close(self) -> None:
        self._writer.close()

    def get_extra_info(self, *args, **kwargs):
        return self._writer.get_extra_info(*args, **kwargs)


# RFB security types (RFC 6143 §7.2).
SEC_INVALID = 0
SEC_NONE = 1
SEC_VNC = 2


class RFBClient:
    """Handles a single VNC client connection.

    Speaks RFB 3.3, 3.7, or 3.8 — minor is negotiated per-connection
    in ``_handshake``. The wire format of the security handshake
    differs between 3.3 and 3.7+; see ``_negotiate_security``.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        server: RFBServer,
    ):
        if server.log_traffic:
            self.reader = _LoggingReader(reader, "<<")
            self.writer = _LoggingWriter(writer, ">>")
        else:
            self.reader = reader
            self.writer = writer
        self.server = server
        self.update_requested = False
        self.encodings: list[int] = [0]  # Raw by default
        self._ctrl_pressed = False
        self._alt_pressed = False
        self._zlib_compressor: zlib.compressobj | None = None

        # Default pixel format matches our ServerInit advertisement (RGBX).
        self.pixel_format = {
            "bpp": 32, "depth": 24, "big_endian": 0, "true_colour": 1,
            "red_max": 255, "green_max": 255, "blue_max": 255,
            "red_shift": 0, "green_shift": 8, "blue_shift": 16,
        }
        self._raw_mode = "RGBX"

        # Pointer state for drag-to-select.
        self._left_down = False
        self._last_pointer_cell: tuple[int, int] | None = None

        # Negotiated RFB minor version (3, 7, or 8). Set by _handshake.
        self.rfb_minor = 8

    async def run(self) -> None:
        await self._handshake()
        await self._message_loop()

    async def _vnc_auth(self) -> bool:
        """Perform VNC Authentication (RFB security type 2)."""
        challenge = secrets.token_bytes(16)
        self.writer.write(challenge)
        await self.writer.drain()
        response = await self.reader.readexactly(16)
        if self.server.password is None:
            return False
        expected = _vnc_encrypt(challenge, self.server.password)
        return hmac.compare_digest(response, expected)

    @staticmethod
    def _parse_rfb_minor(client_version: bytes) -> int:
        # RFC 6143 §6.1.1: any unrecognised 3.x must be treated as 3.3.
        # Anything not parseable, or major != 3, falls back to 3.8 —
        # same policy as xrdp's vnc/vnc.c negotiate_protocol_version.
        if (
            len(client_version) != 12
            or not client_version.startswith(b"RFB ")
            or client_version[7:8] != b"."
            or client_version[11:12] != b"\n"
        ):
            return 8
        try:
            major = int(client_version[4:7])
            minor = int(client_version[8:11])
        except ValueError:
            return 8
        if major != 3:
            return 8
        if minor >= 8:
            return 8
        if minor >= 7:
            return 7
        return 3

    async def _negotiate_security(self, minor: int) -> int:
        # RFB 3.3: server picks the security type and sends a single
        # uint32 — the client has no choice. RFB 3.7+: server sends a
        # count-prefixed list and reads back the client's one-byte pick.
        offered = SEC_VNC if self.server.password else SEC_NONE
        if minor == 3:
            self.writer.write(struct.pack(">I", offered))
            await self.writer.drain()
            _trace("RFB 3.3: server-decided security type %d", offered)
            return offered

        self.writer.write(bytes([1, offered]))
        await self.writer.drain()
        selected = (await self.reader.readexactly(1))[0]
        _trace("client selected security type %d", selected)
        if selected != offered:
            log.warning(
                "RFB: client selected unsupported security type %d; offered [%d]",
                selected,
                offered,
            )
            await self._send_security_result(
                minor, ok=False, reason=b"unsupported security type"
            )
            raise ConnectionError(f"unsupported security type {selected}")
        return selected

    async def _send_security_result(
        self, minor: int, ok: bool, reason: bytes
    ) -> None:
        # RFB 3.8 onwards adds a length-prefixed reason string after a
        # failed SecurityResult. 3.3/3.7 just close the connection.
        self.writer.write(struct.pack(">I", 0 if ok else 1))
        if not ok and minor >= 8:
            self.writer.write(struct.pack(">I", len(reason)) + reason)
        await self.writer.drain()

    async def _handshake(self) -> None:
        peer = self.writer.get_extra_info("peername")
        _trace("handshake start peer=%r", peer)

        # 1. ProtocolVersion: advertise our highest supported minor.
        self.writer.write(b"RFB 003.008\n")
        await self.writer.drain()

        # 2. Read client version and clamp to a minor we speak.
        client_version = bytes(await self.reader.readexactly(12))
        _trace("client version=%r", client_version)
        self.rfb_minor = self._parse_rfb_minor(client_version)
        _trace("negotiated RFB 3.%d", self.rfb_minor)

        # 3. Security handshake (wire format depends on minor).
        selected = await self._negotiate_security(self.rfb_minor)

        # 4. Run auth and send SecurityResult per version semantics.
        #    RFB 3.3/3.7 skip SecurityResult entirely for None auth and
        #    proceed straight to ClientInit. 3.8 sends SecurityResult
        #    for both auth types.
        if selected == SEC_VNC:
            ok = await self._vnc_auth()
            _trace("VNC auth result ok=%s", ok)
            await self._send_security_result(
                self.rfb_minor, ok, b"VNC authentication failed"
            )
            if not ok:
                raise ConnectionError("VNC auth failed")
        elif selected == SEC_NONE:
            if self.rfb_minor >= 8:
                await self._send_security_result(self.rfb_minor, True, b"")
        else:
            raise ConnectionError(f"unexpected security type {selected}")

        # 5. ClientInit (shared flag)
        shared = (await self.reader.readexactly(1))[0]
        _trace("ClientInit shared=%d", shared)

        # 7. ServerInit
        renderer = self.server.renderer
        name = b"vncvt"
        pixel_format = struct.pack(
            ">BBBBHHHBBBxxx",
            32,   # bits-per-pixel
            24,   # depth
            0,    # big-endian
            1,    # true-colour
            255,  # red-max
            255,  # green-max
            255,  # blue-max
            0,    # red-shift   (byte0 = R in RGBX memory layout)
            8,    # green-shift (byte1 = G)
            16,   # blue-shift  (byte2 = B)
        )
        server_init = struct.pack(">HH", renderer.width, renderer.height)
        server_init += pixel_format
        server_init += struct.pack(">I", len(name)) + name
        self.writer.write(server_init)
        await self.writer.drain()

    async def _message_loop(self) -> None:
        """Process client messages indefinitely."""
        while True:
            msg_type = (await self.reader.readexactly(1))[0]
            _trace("msg type=%d", msg_type)

            if msg_type == 0:    # SetPixelFormat
                raw = await self.reader.readexactly(19)  # 3 pad + 16 pf
                pf_bytes = raw[3:]
                (bpp, depth, be, tc,
                 r_max, g_max, b_max,
                 r_shift, g_shift, b_shift) = struct.unpack(
                    ">BBBBHHHBBB", pf_bytes[:13]
                )
                self.pixel_format = {
                    "bpp": bpp, "depth": depth,
                    "big_endian": be, "true_colour": tc,
                    "red_max": r_max, "green_max": g_max, "blue_max": b_max,
                    "red_shift": r_shift, "green_shift": g_shift,
                    "blue_shift": b_shift,
                }
                self._raw_mode = _choose_raw_mode(self.pixel_format)
                log.info("Client pixel format -> raw mode %s", self._raw_mode)

            elif msg_type == 2:  # SetEncodings
                data = await self.reader.readexactly(3)  # 1 pad + 2 count
                n_enc = struct.unpack(">xH", data)[0]
                enc_data = await self.reader.readexactly(n_enc * 4)
                self.encodings = [
                    struct.unpack(">i", enc_data[i * 4 : (i + 1) * 4])[0]
                    for i in range(n_enc)
                ]

            elif msg_type == 3:  # FramebufferUpdateRequest
                data = await self.reader.readexactly(9)
                incremental = data[0]
                if not incremental:
                    # Send full framebuffer from the active screen (terminal
                    # or setup overlay)
                    selection = (
                        None if self.server._in_setup
                        else self.server.terminal.selection_normalized()
                    )
                    fb = self.server.renderer.full_render(
                        self.server._active_screen,
                        selection=selection,
                    )
                    await self._send_full_update(fb)
                else:
                    self.update_requested = True

            elif msg_type == 4:  # KeyEvent
                data = await self.reader.readexactly(7)
                down_flag = data[0]
                keysym = struct.unpack(">I", data[3:7])[0]

                # Track Ctrl state
                if keysym in (0xFFE3, 0xFFE4):  # Control_L, Control_R
                    self._ctrl_pressed = bool(down_flag)
                # Track Alt/Meta state. X11 has separate keysyms for
                # Alt_L/R (0xFFE9/0xFFEA) and Meta_L/R (0xFFE7/0xFFE8);
                # we treat both as "ESC-prefix next char", matching
                # xterm's `metaSendsEscape=true` default.
                if keysym in (0xFFE7, 0xFFE8, 0xFFE9, 0xFFEA):
                    self._alt_pressed = bool(down_flag)

                # F3 = SET-UP mode toggle. Captured on key-down;
                # the shell never sees \x1bOR for F3 anymore.
                if down_flag and keysym == 0xFFC0:  # F3
                    if self.server._in_setup:
                        await self.server.exit_setup(apply=False)
                    else:
                        await self.server.enter_setup()
                    continue

                # In SET-UP mode: Escape applies, other keys go to widget
                if self.server._in_setup:
                    if down_flag:
                        if keysym == 0xFF1B:  # Escape = apply + exit
                            await self.server.exit_setup(apply=True)
                        elif self.server._setup is not None:
                            self.server._setup.on_key(keysym)
                    continue

                if down_flag:
                    byte_seq = keysym_to_bytes(
                        keysym,
                        self._ctrl_pressed,
                        alt_pressed=self._alt_pressed,
                    )
                    if byte_seq:
                        self.server.terminal.write(byte_seq)

            elif msg_type == 5:  # PointerEvent
                data = await self.reader.readexactly(5)
                button_mask, px, py = struct.unpack(">BHH", data)
                left = bool(button_mask & 0x01)
                term = self.server.terminal
                rend = self.server.renderer
                # Subtract overscan padding so clicks in the border
                # clamp to the nearest edge cell.
                col = max(0, min(term.cols - 1, (px - rend.padding) // rend.cell_width))
                row = max(0, min(term.rows - 1, (py - rend.padding) // rend.cell_height))

                if left and not self._left_down:
                    # Button-down edge — start a new selection.
                    term.begin_selection(col, row)
                elif left and self._left_down:
                    # Drag — update the moving end.
                    if self._last_pointer_cell != (col, row):
                        term.update_selection(col, row)
                elif not left and self._left_down:
                    # Button-up edge — finalize and ship cut text.
                    text = term.end_selection()
                    if text:
                        await self._send_cut_text(text)

                self._left_down = left
                self._last_pointer_cell = (col, row)

            elif msg_type == 6:  # ClientCutText
                data = await self.reader.readexactly(7)
                length = struct.unpack(">I", data[3:7])[0]
                text = await self.reader.readexactly(length)
                self.server.terminal.write(text)

            elif msg_type == 251:  # SetDesktopSize
                data = await self.reader.readexactly(7)
                # 1 pad + 2 width + 2 height + 1 num-screens + 1 pad
                req_w, req_h, num_screens = struct.unpack(">xHHBx", data)
                # Read screen entries (16 bytes each)
                for _ in range(num_screens):
                    await self.reader.readexactly(16)

                # Per RFB community wiki §SetDesktopSize: a client that
                # sends msg 251 must advertise pseudo-encoding -308
                # (ExtendedDesktopSize). -223 (DesktopSize) is the
                # legacy server→client notification only; clients that
                # only support -223 cannot resize.
                if -308 not in self.encodings:
                    log.warning(
                        "RFB: client sent SetDesktopSize without "
                        "advertising ExtendedDesktopSize (-308); ignoring"
                    )
                    continue

                renderer = self.server.renderer
                pad = renderer.padding
                new_cols = max(1, (req_w - 2 * pad) // renderer.cell_width)
                new_rows = max(1, (req_h - 2 * pad) // renderer.cell_height)
                log.info(
                    "RFB: client resize request %dx%d px -> %dx%d cells",
                    req_w, req_h, new_cols, new_rows,
                )
                # Centralised handler does the lock + multi-client broadcast.
                await self.server.handle_resize(new_cols, new_rows)

            else:
                # Unknown message types are a protocol error from our
                # perspective. We don't know how many payload bytes
                # follow, so we cannot keep reading — any further
                # readexactly() would interpret payload as a message
                # header and silently desync, producing a "connected
                # but nothing happens" hang. Log loudly and close.
                log.warning(
                    "RFB: unknown client message type %d (0x%02x); "
                    "closing connection to avoid desync",
                    msg_type, msg_type,
                )
                raise ConnectionError(
                    f"unknown RFB message type {msg_type}"
                )

    async def notify_resize(self, width: int, height: int) -> None:
        """Tell this client the framebuffer is now ``width x height`` and
        push a full repaint at the new size.

        Clients that advertised neither -223 (DesktopSize) nor -308
        (ExtendedDesktopSize) have no way to receive the size change,
        so we disconnect them rather than leave them desynced — the
        same fail-loud principle as the RFB dialect fix.
        """
        if -223 not in self.encodings and -308 not in self.encodings:
            raise ConnectionError(
                "client does not support resize notification "
                "(neither DesktopSize nor ExtendedDesktopSize advertised)"
            )
        await self._send_desktop_size(width, height, status=0)
        # The server has already rendered the full framebuffer at the
        # new dimensions before broadcasting; just push it.
        await self._send_full_update(self.server.renderer.image)

    async def _send_desktop_size(
        self, width: int, height: int, status: int = 0
    ) -> None:
        """Notify the client of a new framebuffer size, using whichever
        of the two desktop-size pseudo-encodings the client supports.

        Per the RFB community wiki: "Servers and clients should support
        both for maximum compatibility, but a server must only send the
        extended version to a client asking for both." So:

        - If the client advertised ExtendedDesktopSize (-308), send a
          -308 rect (12-byte header + screen list) — the modern form,
          preferred because it carries multi-screen info and a status
          field for client-initiated requests.
        - Otherwise, if it advertised DesktopSize (-223), send a -223
          rect — just the 12-byte header alone, x/y ignored, w/h carry
          the new size. This is the path Apple's Screen Sharing.app
          takes; it advertises -223 but not -308.

        The caller is responsible for checking that at least one of
        the two pseudo-encodings is in self.encodings before calling
        (notify_resize does this).
        """
        if -308 in self.encodings:
            header = struct.pack(">BxH", 0, 1)  # FBU msg, nrects=1
            rect_header = struct.pack(
                ">HHHHi", status, 0, width, height, -308
            )
            num_screens = struct.pack(">Bxxx", 1)
            screen = struct.pack(">IHHHHI", 0, 0, 0, width, height, 0)
            self.writer.write(header + rect_header + num_screens + screen)
        else:
            # Legacy DesktopSize: just the rect header, no payload.
            header = struct.pack(">BxH", 0, 1)
            rect_header = struct.pack(">HHHHi", 0, 0, width, height, -223)
            self.writer.write(header + rect_header)
        await self.writer.drain()

    async def _send_cut_text(self, text: str) -> None:
        """Send a ServerCutText (RFB msg type 3) with the given text.

        RFC 6143 §7.6.4: message type (1) + 3 pad + u32 length + Latin-1
        payload. Full Unicode requires the Extended Clipboard pseudo-
        encoding (-1063) which is not implemented here; non-Latin-1
        characters are replaced with '?'.
        """
        payload = text.encode("latin-1", errors="replace")
        self.writer.write(struct.pack(">Bxxx", 3) + struct.pack(">I", len(payload)) + payload)
        await self.writer.drain()

    def _image_to_bytes(self, img) -> bytes:
        """Serialize a PIL Image to the client's negotiated pixel format.

        Our framebuffer is RGBX. Pillow's raw packer supports direct
        conversion from RGBX to the X-variant modes (RGBX/BGRX/XRGB/XBGR)
        but not to the A-variant modes (RGBA/BGRA/...). For alpha modes
        we go through an explicit RGBA conversion first.
        """
        mode = self._raw_mode
        if mode in ("RGBX", "BGRX", "XRGB", "XBGR"):
            return img.tobytes("raw", mode)
        return img.convert("RGBA").tobytes("raw", mode)

    async def _send_full_update(self, fb_image) -> None:
        """Send a non-incremental full framebuffer update.

        Uses a drain() timeout to avoid wedging the server if a client
        stops reading mid-transfer (test clients that only drive the
        handshake are a common offender — they never drain the FBU,
        so a naive await drain() blocks the whole update loop until
        the TCP connection times out).
        """
        renderer = self.server.renderer
        pixel_data = self._image_to_bytes(fb_image)
        header = struct.pack(">BxH", 0, 1)  # type=0, 1 rectangle
        rect_header = struct.pack(
            ">HHHHi", 0, 0, renderer.width, renderer.height, 0  # Raw encoding
        )
        self.writer.write(header + rect_header + pixel_data)
        try:
            await asyncio.wait_for(self.writer.drain(), timeout=2.0)
        except asyncio.TimeoutError:
            raise ConnectionError(
                "client not draining framebuffer update within 2s"
            )

    async def send_framebuffer_update(self, rects) -> None:
        """Send incremental framebuffer update with given rectangles.

        Each rect is (x, y, w, h, PIL.Image). Pixel bytes are serialized
        per-client using the negotiated pixel format.
        """
        use_zlib = 6 in self.encodings  # zlib encoding type
        header = struct.pack(">BxH", 0, len(rects))
        self.writer.write(header)

        for x, y, w, h, img in rects:
            pixel_data = self._image_to_bytes(img)
            if use_zlib:
                if self._zlib_compressor is None:
                    self._zlib_compressor = zlib.compressobj()
                compressed = self._zlib_compressor.compress(pixel_data)
                compressed += self._zlib_compressor.flush(zlib.Z_SYNC_FLUSH)
                rect_header = struct.pack(">HHHHi", x, y, w, h, 6)  # zlib
                self.writer.write(rect_header)
                self.writer.write(struct.pack(">I", len(compressed)))
                self.writer.write(compressed)
            else:
                rect_header = struct.pack(">HHHHi", x, y, w, h, 0)  # Raw
                self.writer.write(rect_header)
                self.writer.write(pixel_data)

        await self.writer.drain()
        self.update_requested = False
