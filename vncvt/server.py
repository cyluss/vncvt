"""RFB (VNC) protocol server with asyncio."""

import asyncio
import logging
import struct
import zlib

from .terminal import Terminal
from .renderer import TerminalRenderer

log = logging.getLogger(__name__)


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
    ):
        self.host = host
        self.port = port
        self.terminal = terminal
        self.renderer = renderer
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

    async def run(self) -> None:
        await self._handshake()
        await self._message_loop()

    async def _handshake(self) -> None:
        """Perform RFB 3.8 protocol handshake."""
        # 1. Protocol version
        self.writer.write(b"RFB 003.008\n")
        await self.writer.drain()

        # 2. Read client version
        await self.reader.readexactly(12)

        # 3. Security types: offer None (type 1)
        self.writer.write(bytes([1, 1]))  # 1 security type, type=None
        await self.writer.drain()

        # 4. Read selected security type
        await self.reader.readexactly(1)

        # 5. SecurityResult: OK
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
                await self.reader.readexactly(19)  # 3 pad + 16 format bytes

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

            else:
                log.warning("Unknown message type: %d", msg_type)

    async def _send_full_update(self, fb_data: bytes) -> None:
        """Send a non-incremental full framebuffer update."""
        renderer = self.server.renderer
        header = struct.pack(">BxH", 0, 1)  # type=0, 1 rectangle
        rect_header = struct.pack(
            ">HHHHi", 0, 0, renderer.width, renderer.height, 0  # Raw encoding
        )
        self.writer.write(header + rect_header + fb_data)
        await self.writer.drain()

    async def send_framebuffer_update(
        self, rects: list[tuple[int, int, int, int, bytes]]
    ) -> None:
        """Send incremental framebuffer update with given rectangles."""
        use_zlib = 6 in self.encodings  # zlib encoding type
        header = struct.pack(">BxH", 0, len(rects))
        self.writer.write(header)

        for x, y, w, h, pixel_data in rects:
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
