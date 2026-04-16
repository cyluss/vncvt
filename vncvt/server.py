"""RFB (VNC) protocol server with asyncio."""

import asyncio
import logging

from .keysym import keysym_to_bytes  # noqa: F401 – re-exported for back-compat
from .rfb_client import RFBClient  # noqa: F401 – re-exported for back-compat
from .rfb_client import _vnc_encrypt  # noqa: F401 – re-exported for back-compat
from .terminal import Terminal
from .renderer import TerminalRenderer

log = logging.getLogger(__name__)


class RFBServer:
    """Asyncio-based RFB (VNC) server wiring Terminal and Renderer together."""

    def __init__(
        self,
        host: str,
        port: int,
        terminal: Terminal,
        renderer: TerminalRenderer,
        password: str | None = None,
        log_traffic: bool = False,
        fps: int = 30,
        theme: str = "amber",
    ):
        self.host = host
        self.port = port
        self.terminal = terminal
        self.renderer = renderer
        self.password = password
        self.log_traffic = log_traffic
        self.fps = fps
        # Track the active theme name explicitly. Previously we
        # reverse-guessed by matching DEFAULT_BG to THEMES[name]["bg"],
        # but amber/dark/green all share (0,0,0) bg so the match was
        # ambiguous and enter_setup always seeded the Theme field with
        # "amber" regardless of the actual active theme.
        self.theme = theme
        self.clients: list[RFBClient] = []
        self._running = False
        # Serializes resizes against the render+send pass in
        # _update_loop so a client can't read pixel data from a
        # half-reallocated framebuffer mid-FBU.
        self._resize_lock = asyncio.Lock()
        # Phase 2: active screen indirection for SET-UP mode overlay.
        # Points to terminal.screen by default; swaps to setup_screen.screen
        # when _in_setup is True.
        self._active_screen = terminal.screen
        self._in_setup = False
        self._setup = None  # type: ignore[assignment]

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
        self.renderer.render_dirty(self._active_screen, all_rows)
        self._active_screen.dirty.clear()

        loop = asyncio.get_event_loop()
        loop.add_reader(self.terminal.master_fd, self._on_pty_data)
        asyncio.create_task(self._update_loop())

        await server.serve_forever()

    def _on_pty_data(self) -> None:
        """Callback when PTY master fd has data ready."""
        data = self.terminal.read()
        if data:
            self.terminal.feed(data)

    def _get_and_clear_dirty_rows(self) -> set[int]:
        """Return dirty rows from the active screen and clear its dirty set."""
        if self._in_setup and self._setup is not None:
            dirty = set(self._setup.screen.dirty)
            self._setup.screen.dirty.clear()
            return dirty
        return self.terminal.get_dirty_rows()

    async def _update_loop(self) -> None:
        """Periodic loop: render dirty rows and push to clients."""
        while self._running:
            await asyncio.sleep(1 / max(1, self.fps))

            # Hold the resize lock across the whole render+send pass so
            # a concurrent handle_resize cannot reallocate the framebuffer
            # mid-flight. The lock is uncontended in steady state.
            async with self._resize_lock:
                dirty = self._get_and_clear_dirty_rows()
                if not dirty and not self.clients:
                    continue

                # Selection only applies to the live terminal screen
                selection = (
                    None if self._in_setup
                    else self.terminal.selection_normalized()
                )
                # Render on a worker thread so the event loop stays
                # responsive for PTY reads, new connections, and
                # control-socket traffic while Skia rasterizes glyphs.
                # The _resize_lock is still held so the framebuffer
                # isn't mutated concurrently by handle_resize.
                rects = []
                if dirty:
                    rects = await asyncio.to_thread(
                        self.renderer.render_dirty,
                        self._active_screen, dirty, selection,
                    )

                # Always update cursor position
                cursor_rect = await asyncio.to_thread(
                    self.renderer.render_cursor, self._active_screen,
                )
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

    async def enter_setup(self) -> None:
        """Swap to the SET-UP mode overlay. Safe to call from any coro."""
        from .setup_screen import SetupScreen

        if self._in_setup:
            return
        try:
            import importlib.metadata
            version = importlib.metadata.version("vncvt")
        except Exception:
            version = "0.1.0"

        # Use the tracked theme attribute. Don't reverse-guess from
        # DEFAULT_BG — amber/dark/green all share (0,0,0) bg and the
        # lookup was ambiguous, always falling back to "amber" first
        # match in the dict.
        current_theme = self.theme

        async with self._resize_lock:
            self._setup = SetupScreen(
                cols=self.terminal.cols,
                rows=self.terminal.rows,
                initial={
                    "cols": self.terminal.cols,
                    "rows": self.terminal.rows,
                    "font_size": self.renderer.font_size,
                    "fps": self.fps,
                    "theme": current_theme,
                    "line_height": self.renderer.line_height,
                    "contrast": self.renderer.contrast,
                    "color_mode": self.renderer.color_mode,
                },
                server_info={
                    "version": f"vncvt {version}",
                    "clients": len(self.clients),
                },
            )
            self._in_setup = True
            self._active_screen = self._setup.screen
            # Render immediately so first FBU pass has pixels
            all_rows = set(range(self.terminal.rows))
            self.renderer.render_dirty(self._active_screen, all_rows)
            self._active_screen.dirty.clear()
            # Mark all rows dirty via the helper path — the update loop
            # will pick them up on the next tick.
            self._setup.screen.dirty.update(range(self.terminal.rows))

    async def exit_setup(self, apply: bool) -> None:
        """Leave SET-UP mode. If apply=True, write the snapshot's values
        back to the live server."""
        if not self._in_setup or self._setup is None:
            return
        snap = self._setup.snapshot() if apply else None
        async with self._resize_lock:
            self._in_setup = False
            self._active_screen = self.terminal.screen
            self._setup = None
            # Force a full re-render of the live terminal
            all_rows = set(range(self.terminal.rows))
            self.renderer.render_dirty(self._active_screen, all_rows)
            self._active_screen.dirty.clear()
            self.terminal.screen.dirty.update(range(self.terminal.rows))
        if snap is not None:
            await self._apply_setup_snapshot(snap)

    async def _apply_setup_snapshot(self, snap: dict) -> None:
        """Apply SET-UP mode changes: fps, theme, font size + line
        height, cols/rows."""
        from .renderer import apply_theme
        from .theme import ThemeContext

        # fps first — cheapest, just an attribute write
        new_fps = snap.get("FPS")
        if isinstance(new_fps, int):
            self.fps = new_fps

        # Theme — swap the module-level palette constants and get a
        # ThemeContext to pass to the new renderer.
        new_theme = snap.get("Theme")
        theme_ctx = None
        if isinstance(new_theme, str):
            try:
                theme_ctx = apply_theme(new_theme)
                self.theme = new_theme
            except ValueError:
                pass

        # Font size + line height + contrast — rebuild the renderer
        new_font = snap.get("Font size")
        new_lh = snap.get("Line height")
        new_contrast = snap.get("Contrast")
        new_color_mode = snap.get("Color mode")
        font_changed = (
            isinstance(new_font, int) and new_font != self.renderer.font_size
        )
        lh_changed = (
            isinstance(new_lh, (int, float))
            and abs(new_lh - self.renderer.line_height) > 1e-6
        )
        contrast_changed = (
            isinstance(new_contrast, str)
            and new_contrast != self.renderer.contrast
        )
        color_mode_changed = (
            isinstance(new_color_mode, str)
            and new_color_mode != self.renderer.color_mode
        )
        theme_changed = isinstance(new_theme, str)
        if font_changed or lh_changed or theme_changed or contrast_changed or color_mode_changed:
            # If no theme change happened, carry forward the current
            # renderer's ThemeContext so the new renderer stays isolated.
            if theme_ctx is None:
                theme_ctx = self.renderer.theme
            async with self._resize_lock:
                old_renderer = self.renderer
                self.renderer = TerminalRenderer(
                    cols=self.terminal.cols,
                    rows=self.terminal.rows,
                    font_path=old_renderer._font_path,
                    font_size=new_font if font_changed else old_renderer.font_size,
                    line_height=new_lh if lh_changed else old_renderer.line_height,
                    contrast=new_contrast if contrast_changed else old_renderer.contrast,
                    color_mode=new_color_mode if color_mode_changed else old_renderer.color_mode,
                    theme=theme_ctx,
                )
                all_rows = set(range(self.terminal.rows))
                self.renderer.render_dirty(self._active_screen, all_rows)
                self._active_screen.dirty.clear()
                new_w = self.renderer.width
                new_h = self.renderer.height
                size_changed = (
                    new_w != old_renderer.width
                    or new_h != old_renderer.height
                )
                for client in list(self.clients):
                    try:
                        if size_changed:
                            await client.notify_resize(new_w, new_h)
                        else:
                            # Theme-only change: dimensions unchanged, so
                            # don't send a DesktopSize message (Screen
                            # Sharing treats same-dimension resize as
                            # a no-op and may skip the following frame
                            # buffer update). Push the full image
                            # directly instead.
                            await client._send_full_update(self.renderer.image)
                    except (ConnectionError, OSError):
                        if client in self.clients:
                            self.clients.remove(client)

        # cols/rows last — handle_resize takes its own lock
        new_cols = snap.get("Columns")
        new_rows = snap.get("Rows")
        if (
            isinstance(new_cols, int) and isinstance(new_rows, int)
            and (new_cols != self.terminal.cols or new_rows != self.terminal.rows)
        ):
            await self.handle_resize(new_cols, new_rows)

    async def handle_resize(self, cols: int, rows: int) -> tuple[int, int]:
        """Resize the shared terminal + renderer and notify all
        connected clients. Returns the actual ``(cols, rows)`` after
        clamping. Safe to call from any coroutine — the lock keeps it
        from racing with the render loop."""
        async with self._resize_lock:
            cols = max(1, cols)
            rows = max(1, rows)
            self.terminal.resize(cols, rows)
            self.renderer.resize(cols, rows)
            # Render the whole new framebuffer once so subsequent FBUs
            # have valid pixel data to read.
            all_rows = set(range(rows))
            self.renderer.render_dirty(self._active_screen, all_rows)
            self._active_screen.dirty.clear()

            new_w = self.renderer.width
            new_h = self.renderer.height
            for client in list(self.clients):
                try:
                    await client.notify_resize(new_w, new_h)
                except (ConnectionError, OSError) as e:
                    log.info("client dropped during resize broadcast: %s", e)
                    if client in self.clients:
                        self.clients.remove(client)
            return cols, rows

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
        except Exception:
            log.exception("Client %s handler crashed", peer)
        finally:
            if client in self.clients:
                self.clients.remove(client)
            writer.close()

    def shutdown(self) -> None:
        self._running = False

    async def shutdown_with_notice(self, message: str) -> None:
        """Render a final message into the terminal screen, flush it to
        clients, then shut down. Call this when the shell exits so the
        user sees a clean goodbye instead of an abrupt disconnect."""
        # Feed the message directly into pyte via the terminal's stream.
        # Clear to end of line + set bright yellow + message + reset.
        notice = f"\r\n\x1b[2K\x1b[1;33m{message}\x1b[0m\r\n"
        try:
            self.terminal.stream.feed(notice)
        except Exception:
            pass
        # Mark all rows dirty so the whole screen re-renders
        self.terminal.screen.dirty.update(range(self.terminal.rows))
        # Give the render loop a few ticks to push the update
        for _ in range(5):
            await asyncio.sleep(1 / max(1, self.fps))
        # Linger briefly so the user can read the message
        await asyncio.sleep(1.0)
        self._running = False
