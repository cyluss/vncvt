"""Asciinema v2 .cast recorder.

Writes the `.cast` format documented at
https://docs.asciinema.org/manual/asciicast/v2/ — a JSON header line
followed by newline-delimited JSON event arrays of the form
``[elapsed_seconds, "o"|"i", text]``. The resulting file replays
through ``asciinema play``, renders to GIF via ``agg``, or to SVG via
``svg-term-cli`` — no vncvt-specific tooling required.

We only record the PTY output stream by default. Input recording is
off because it doubles file size and most downstream renderers
ignore the `i` events anyway.
"""

from __future__ import annotations

import codecs
import json
import os
import time
from pathlib import Path
from typing import TextIO


class CastRecorder:
    def __init__(
        self,
        path: Path,
        cols: int,
        rows: int,
        shell: str,
        record_input: bool = False,
    ) -> None:
        self.path = Path(path)
        self.cols = cols
        self.rows = rows
        self._record_input = record_input
        self._start = time.monotonic()
        # Decode PTY bytes incrementally so we never split a multi-
        # byte UTF-8 sequence across two events — asciinema replayers
        # choke on invalid JSON strings.
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fp: TextIO = self.path.open("w", encoding="utf-8")
        header = {
            "version": 2,
            "width": cols,
            "height": rows,
            "timestamp": int(time.time()),
            "env": {
                "SHELL": shell,
                "TERM": os.environ.get("TERM", "xterm-256color"),
            },
        }
        self._fp.write(json.dumps(header) + "\n")
        self._fp.flush()

    def record_output(self, data: bytes) -> None:
        if not data:
            return
        text = self._decoder.decode(data)
        if not text:
            return
        elapsed = time.monotonic() - self._start
        self._fp.write(json.dumps([elapsed, "o", text]) + "\n")
        self._fp.flush()

    def record_input(self, data: bytes) -> None:
        if not self._record_input or not data:
            return
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return
        elapsed = time.monotonic() - self._start
        self._fp.write(json.dumps([elapsed, "i", text]) + "\n")
        self._fp.flush()

    def resize(self, cols: int, rows: int) -> None:
        """Emit a resize event. asciinema v2 uses an `r` event with
        ``"<cols>x<rows>"`` as the payload."""
        self.cols = cols
        self.rows = rows
        elapsed = time.monotonic() - self._start
        self._fp.write(json.dumps([elapsed, "r", f"{cols}x{rows}"]) + "\n")
        self._fp.flush()

    def close(self) -> None:
        try:
            self._fp.close()
        except Exception:
            pass
