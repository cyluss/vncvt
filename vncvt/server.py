"""RFB (VNC) protocol server with asyncio."""

import asyncio
import hmac
import logging
import secrets
import struct
import zlib

from .terminal import Terminal
from .renderer import TerminalRenderer

log = logging.getLogger(__name__)


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


def keysym_to_bytes(keysym: int, ctrl_pressed: bool) -> bytes | None:
    """Convert an X11 keysym to the byte sequence to write to a terminal PTY."""
    # Modifier keys produce no output
    if 0xFFE1 <= keysym <= 0xFFEE:
        return None

    # Ctrl+letter
    if ctrl_pressed:
        if 0x61 <= keysym <= 0x7A:  # a-z
            return bytes([keysym - 0x60])
        if 0x41 <= keysym <= 0x5A:  # A-Z
            return bytes([keysym - 0x40])
        # Ctrl+[ = Escape, Ctrl+\ = 0x1c, etc.
        if keysym in (0x5B, 0x5C, 0x5D, 0x5E, 0x5F):
            return bytes([keysym - 0x40])

    # ASCII printable
    if 0x20 <= keysym <= 0x7E:
        return bytes([keysym])

    # Special keys
    _SPECIAL: dict[int, bytes] = {
        0xFF08: b"\x7f",       # BackSpace
        0xFF09: b"\t",         # Tab
        0xFF0D: b"\r",         # Return
        0xFF0A: b"\r",         # Linefeed (some clients)
        0xFF1B: b"\x1b",       # Escape
        0xFFFF: b"\x1b[3~",    # Delete
        0xFF63: b"\x1b[2~",    # Insert
        0xFF50: b"\x1b[H",     # Home
        0xFF57: b"\x1b[F",     # End
        0xFF55: b"\x1b[5~",    # Page_Up
        0xFF56: b"\x1b[6~",    # Page_Down
        0xFF51: b"\x1b[D",     # Left
        0xFF52: b"\x1b[A",     # Up
        0xFF53: b"\x1b[C",     # Right
        0xFF54: b"\x1b[B",     # Down
        # F1-F12
        0xFFBE: b"\x1bOP",     0xFFBF: b"\x1bOQ",
        0xFFC0: b"\x1bOR",     0xFFC1: b"\x1bOS",
        0xFFC2: b"\x1b[15~",   0xFFC3: b"\x1b[17~",
        0xFFC4: b"\x1b[18~",   0xFFC5: b"\x1b[19~",
        0xFFC6: b"\x1b[20~",   0xFFC7: b"\x1b[21~",
        0xFFC8: b"\x1b[23~",   0xFFC9: b"\x1b[24~",
    }
    if keysym in _SPECIAL:
        return _SPECIAL[keysym]

    # Unicode keysym (RFB 3.8 extension)
    if keysym >= 0x01000000:
        return chr(keysym - 0x01000000).encode("utf-8")

    # Latin-1 supplement
    if 0x00A0 <= keysym <= 0x00FF:
        return chr(keysym).encode("utf-8")

    return None


class RFBServer:
    """Asyncio-based RFB (VNC) server wiring Terminal and Renderer together."""

    def __init__(
        self,
        host: str,
        port: int,
        terminal: Terminal,
        renderer: TerminalRenderer,
        password: str | None = None,
    ):
        self.host = host
        self.port = port
        self.terminal = terminal
        self.renderer = renderer
        self.password = password
        self.clients: list[RFBClient] = []
        self._running = False

    async def start(self) -> None:
        """Start the VNC server, PTY reader, and update loop."""
        self._running = True
        server = await asyncio.start_server(
            self._handle_connection, self.host, self.port
        )
        addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
        log.info("VNC server listening on %s", addrs)
        print(f"VNC server listening on {addrs}")

        # Do an initial full render so first client gets content immediately
        all_rows = set(range(self.terminal.rows))
        self.renderer.render_dirty(self.terminal.screen, all_rows)
        self.terminal.screen.dirty.clear()

        loop = asyncio.get_event_loop()
        loop.add_reader(self.terminal.master_fd, self._on_pty_data)
        asyncio.create_task(self._update_loop())

        await server.serve_forever()

    def _on_pty_data(self) -> None:
        """Callback when PTY master fd has data ready."""
        data = self.terminal.read()
        if data:
            self.terminal.feed(data)

    async def _update_loop(self) -> None:
        """Periodic loop: render dirty rows and push to clients."""
        while self._running:
            await asyncio.sleep(1 / 30)  # ~30 fps cap

            dirty = self.terminal.get_dirty_rows()
            if not dirty and not self.clients:
                continue

            rects = []
            if dirty:
                rects = self.renderer.render_dirty(self.terminal.screen, dirty)

            # Always update cursor position
            cursor_rect = self.renderer.render_cursor(self.terminal.screen)
            if cursor_rect:
                rects.append(cursor_rect)

            if not rects:
                continue

            for client in list(self.clients):
                if client.update_requested:
                    try:
                        await client.send_framebuffer_update(rects)
                    except (ConnectionError, OSError):
                        self.clients.remove(client)

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        log.info("Client connected: %s", peer)
        client = RFBClient(reader, writer, self)
        self.clients.append(client)
        try:
            await client.run()
        except (ConnectionError, asyncio.IncompleteReadError, OSError) as e:
            log.info("Client %s disconnected: %s", peer, e)
        finally:
            if client in self.clients:
                self.clients.remove(client)
            writer.close()

    def shutdown(self) -> None:
        self._running = False


class RFBClient:
    """Handles a single VNC client connection using the RFB 3.8 protocol."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        server: RFBServer,
    ):
        self.reader = reader
        self.writer = writer
        self.server = server
        self.update_requested = False
        self.encodings: list[int] = [0]  # Raw by default
        self._ctrl_pressed = False
        self._zlib_compressor: zlib.compressobj | None = None

        # Default pixel format matches our ServerInit advertisement (RGBX).
        self.pixel_format = {
            "bpp": 32, "depth": 24, "big_endian": 0, "true_colour": 1,
            "red_max": 255, "green_max": 255, "blue_max": 255,
            "red_shift": 0, "green_shift": 8, "blue_shift": 16,
        }
        self._raw_mode = "RGBX"

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

    async def _handshake(self) -> None:
        """Perform RFB 3.8 protocol handshake."""
        # 1. Protocol version
        self.writer.write(b"RFB 003.008\n")
        await self.writer.drain()

        # 2. Read client version
        await self.reader.readexactly(12)

        # 3. Security types: offer VNC auth (2) + None (1) if password is
        #    configured; otherwise offer only None.
        if self.server.password:
            self.writer.write(bytes([2, 2, 1]))  # 2 types: VNC, None
        else:
            self.writer.write(bytes([1, 1]))     # 1 type:  None
        await self.writer.drain()

        # 4. Read selected security type
        selected = (await self.reader.readexactly(1))[0]

        # 5. Run auth for the selected type, then send SecurityResult.
        if selected == 2:
            ok = await self._vnc_auth()
            if not ok:
                self.writer.write(struct.pack(">I", 1))
                reason = b"VNC authentication failed"
                self.writer.write(struct.pack(">I", len(reason)) + reason)
                await self.writer.drain()
                raise ConnectionError("VNC auth failed")
            self.writer.write(struct.pack(">I", 0))
        else:
            self.writer.write(struct.pack(">I", 0))
        await self.writer.drain()

        # 6. ClientInit (shared flag)
        await self.reader.readexactly(1)

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
                    # Send full framebuffer
                    fb = self.server.renderer.full_render(
                        self.server.terminal.screen
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

                if down_flag:
                    byte_seq = keysym_to_bytes(keysym, self._ctrl_pressed)
                    if byte_seq:
                        self.server.terminal.write(byte_seq)

            elif msg_type == 5:  # PointerEvent
                await self.reader.readexactly(5)

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

                if -223 in self.encodings:
                    renderer = self.server.renderer
                    new_cols = max(1, req_w // renderer.cell_width)
                    new_rows = max(1, req_h // renderer.cell_height)
                    # Snap to cell grid
                    actual_w = new_cols * renderer.cell_width
                    actual_h = new_rows * renderer.cell_height
                    self.server.terminal.resize(new_cols, new_rows)
                    renderer.resize(new_cols, new_rows)
                    log.info(
                        "Resize: %dx%d -> %dx%d cols/rows, %dx%d px",
                        req_w, req_h, new_cols, new_rows, actual_w, actual_h,
                    )
                    await self._send_desktop_size(actual_w, actual_h, status=0)
                    # Send full framebuffer at new size
                    fb = renderer.full_render(self.server.terminal.screen)
                    await self._send_full_update(fb)

            else:
                log.warning("Unknown message type: %d", msg_type)

    async def _send_desktop_size(self, width: int, height: int, status: int = 0) -> None:
        """Send ExtendedDesktopSize pseudo-encoding to confirm resize."""
        # FramebufferUpdate with 1 rect using encoding -308 (ExtendedDesktopSize)
        # status: 0=ok, x/y encodes the status and reason
        header = struct.pack(">BxH", 0, 1)
        # x=status, y=0 (server-requested change)
        rect_header = struct.pack(">HHHHi", status, 0, width, height, -308)
        # 1 screen: id=0, x=0, y=0, width, height, flags=0
        screen = struct.pack(">IHHHHI", 0, 0, 0, width, height, 0)
        num_screens = struct.pack(">Bxxx", 1)
        self.writer.write(header + rect_header + num_screens + screen)
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
        """Send a non-incremental full framebuffer update."""
        renderer = self.server.renderer
        pixel_data = self._image_to_bytes(fb_image)
        header = struct.pack(">BxH", 0, 1)  # type=0, 1 rectangle
        rect_header = struct.pack(
            ">HHHHi", 0, 0, renderer.width, renderer.height, 0  # Raw encoding
        )
        self.writer.write(header + rect_header + pixel_data)
        await self.writer.drain()

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
