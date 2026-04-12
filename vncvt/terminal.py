"""PTY management and pyte terminal emulation."""

import codecs
import fcntl
import os
import select
import signal
import struct
import termios

import pyte
import wcwidth


class Terminal:
    """Manages a bash process in a PTY and a pyte virtual terminal screen."""

    def __init__(self, cols: int = 80, rows: int = 24, shell: str = "/bin/bash"):
        self.cols = cols
        self.rows = rows
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.Stream(self.screen)
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

        # Text selection state (in cell coordinates).
        self.selection_anchor: tuple[int, int] | None = None  # (col, row)
        self.selection_head: tuple[int, int] | None = None

        # Track cursor position so we can dirty the previous row when
        # the cursor moves — otherwise a ghost cursor lingers on the
        # old row (e.g. after pressing Enter, the prompt's tail row
        # keeps showing the block cursor).
        self._last_cursor: tuple[int, int] = (
            self.screen.cursor.x,
            self.screen.cursor.y,
        )

        self.master_fd, slave_fd = os.openpty()

        # Set terminal size on slave before fork
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

        self.pid = os.fork()
        if self.pid == 0:
            # Child process
            os.close(self.master_fd)
            os.setsid()
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)
            os.environ["TERM"] = "xterm-256color"
            os.environ["COLUMNS"] = str(cols)
            os.environ["LINES"] = str(rows)
            os.execvp(shell, [shell])
        else:
            # Parent process
            os.close(slave_fd)
            # Set master fd to non-blocking
            flags = fcntl.fcntl(self.master_fd, fcntl.F_GETFL)
            fcntl.fcntl(self.master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def read(self) -> bytes:
        """Non-blocking read from PTY master. Returns b'' if nothing available."""
        ready, _, _ = select.select([self.master_fd], [], [], 0)
        if not ready:
            return b""
        try:
            return os.read(self.master_fd, 65536)
        except OSError:
            return b""

    def feed(self, data: bytes) -> None:
        """Decode bytes and feed to pyte stream."""
        text = self._decoder.decode(data)
        if text:
            # Any new PTY output cancels an in-progress selection.
            if self.selection_anchor is not None:
                self.clear_selection()
            self.stream.feed(text)

    def write(self, data: bytes) -> None:
        """Write data to PTY master (keyboard input to bash)."""
        try:
            os.write(self.master_fd, data)
        except OSError:
            pass

    def resize(self, cols: int, rows: int) -> None:
        """Resize terminal and PTY."""
        self.cols = cols
        self.rows = rows
        self.screen.resize(rows, cols)
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
        os.kill(self.pid, signal.SIGWINCH)

    def get_dirty_rows(self) -> set[int]:
        """Return set of dirty row indices and clear the dirty set.

        Also adds the previous cursor row when the cursor has moved,
        so the old cursor block gets erased on the next render pass.
        """
        dirty = self.screen.dirty.copy()
        cx, cy = self.screen.cursor.x, self.screen.cursor.y
        if (cx, cy) != self._last_cursor:
            prev_y = self._last_cursor[1]
            if 0 <= prev_y < self.rows:
                dirty.add(prev_y)
            if 0 <= cy < self.rows:
                dirty.add(cy)
            self._last_cursor = (cx, cy)
        self.screen.dirty.clear()
        return dirty

    # ----- Text selection -----

    def _mark_selection_dirty(self) -> None:
        """Mark rows spanned by the current selection as dirty."""
        if self.selection_anchor is None or self.selection_head is None:
            return
        r1 = min(self.selection_anchor[1], self.selection_head[1])
        r2 = max(self.selection_anchor[1], self.selection_head[1])
        for r in range(max(0, r1), min(self.rows, r2 + 1)):
            self.screen.dirty.add(r)

    def begin_selection(self, col: int, row: int) -> None:
        """Start a new selection at the given cell."""
        self._mark_selection_dirty()  # repaint old highlight (if any)
        self.selection_anchor = (col, row)
        self.selection_head = (col, row)
        self._mark_selection_dirty()  # paint new highlight

    def update_selection(self, col: int, row: int) -> None:
        """Update the moving end of the selection."""
        if self.selection_anchor is None:
            return
        self._mark_selection_dirty()  # repaint old
        self.selection_head = (col, row)
        self._mark_selection_dirty()  # paint new

    def end_selection(self) -> str:
        """Finalize selection and return the selected text.

        The highlight remains visible until a new selection starts or
        PTY output arrives — that way the user sees what was copied.
        """
        return self.get_selection_text()

    def clear_selection(self) -> None:
        """Drop any active selection and repaint affected rows."""
        if self.selection_anchor is None:
            return
        self._mark_selection_dirty()
        self.selection_anchor = None
        self.selection_head = None

    def selection_normalized(
        self,
    ) -> tuple[tuple[int, int], tuple[int, int]] | None:
        """Return the selection as (start, end) in reading order, or None."""
        if self.selection_anchor is None or self.selection_head is None:
            return None
        a = self.selection_anchor
        b = self.selection_head
        if (a[1], a[0]) <= (b[1], b[0]):
            return a, b
        return b, a

    def get_selection_text(self) -> str:
        """Extract the currently-selected text from the pyte buffer."""
        sel = self.selection_normalized()
        if sel is None:
            return ""
        (sc, sr), (ec, er) = sel
        lines: list[str] = []
        for row in range(sr, er + 1):
            if row >= self.rows:
                break
            line = self.screen.buffer[row]
            if row == sr and row == er:
                c1, c2 = sc, ec
            elif row == sr:
                c1, c2 = sc, self.cols - 1
            elif row == er:
                c1, c2 = 0, ec
            else:
                c1, c2 = 0, self.cols - 1

            chars: list[str] = []
            col = c1
            while col <= c2:
                ch = line[col].data or " "
                chars.append(ch)
                # Skip the shadow cell of a double-wide glyph so CJK
                # characters aren't duplicated in the output.
                if wcwidth.wcwidth(ch) == 2:
                    col += 2
                else:
                    col += 1
            lines.append("".join(chars).rstrip())
        return "\n".join(lines)

    def alive(self) -> bool:
        """Check if the child process is still running."""
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            return pid == 0
        except ChildProcessError:
            return False

    def close(self) -> None:
        """Kill child process and close PTY."""
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass
