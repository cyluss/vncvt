"""PTY management and pyte terminal emulation."""

import codecs
import fcntl
import os
import select
import signal
import struct
import termios

import pyte


class Terminal:
    """Manages a bash process in a PTY and a pyte virtual terminal screen."""

    def __init__(self, cols: int = 80, rows: int = 24, shell: str = "/bin/bash"):
        self.cols = cols
        self.rows = rows
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.Stream(self.screen)
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

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
        """Return set of dirty row indices and clear the dirty set."""
        dirty = self.screen.dirty.copy()
        self.screen.dirty.clear()
        return dirty

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
