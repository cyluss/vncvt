"""VT220-style SET-UP mode overlay.

A ``SetupScreen`` owns its own ``pyte.Screen`` + ``Stream`` that the
existing ``TerminalRenderer`` draws for free. Escape sequences are
fed into the stream to produce the layout; reverse-video SGR marks
the selected row.

The overlay is a widget: it knows nothing about VNC, RFB, or the
server. ``RFBServer.enter_setup`` / ``exit_setup`` in Phase 2 handles
the lifecycle and apply-on-exit.
"""

from __future__ import annotations

from typing import Any

import pyte


# Key sym constants mirrored from server.keysym_to_bytes for navigation.
_KEY_UP = 0xFF52
_KEY_DOWN = 0xFF54
_KEY_LEFT = 0xFF51
_KEY_RIGHT = 0xFF53
_KEY_RETURN = 0xFF0D
_KEY_ENTER = 0xFF8D


class SetupField:
    """One configurable row in the SET-UP screen.

    A field has a label, a list of cycleable values, and a current
    index. Readonly fields display a single value and don't respond
    to cycle().
    """

    def __init__(
        self,
        label: str,
        values: list[Any],
        index: int = 0,
        readonly: bool = False,
    ):
        if not values:
            raise ValueError(f"field {label!r} must have at least one value")
        self.label = label
        self.values = values
        self.index = max(0, min(index, len(values) - 1))
        self.readonly = readonly

    def current(self) -> Any:
        return self.values[self.index]

    def cycle(self, delta: int = 1) -> None:
        if self.readonly:
            return
        self.index = (self.index + delta) % len(self.values)


class SetupScreen:
    """VT220 SET-UP mode overlay widget.

    Owns a pyte.Screen + Stream pair the outer renderer draws. The
    overlay is laid out as:

        ─── VNCVT SET-UP ─────────────────────────────────────
        Columns       : [80]    80, 132
        Rows          : [24]    24, 36, 48
        Font size     : [11]    8-32
        FPS           : [30]    15, 30, 60, 90, 120
        Cursor        : [block] block, underline, bar
        ─── Info ─────────────────────────────────────────────
        Server        : vncvt 0.1.0
        Clients       : 1
        ──────────────────────────────────────────────────────
        Arrows navigate · Return cycles · Esc applies · F3 cancels
    """

    def __init__(
        self,
        cols: int,
        rows: int,
        initial: dict[str, Any] | None = None,
        server_info: dict[str, Any] | None = None,
    ):
        self.cols = cols
        self.rows = rows
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.Stream(self.screen)
        initial = initial or {}
        server_info = server_info or {}

        # Editable fields
        def _field(label: str, values: list[Any], current: Any) -> SetupField:
            try:
                idx = values.index(current)
            except ValueError:
                idx = 0
            return SetupField(label, values, idx)

        self.fields: list[SetupField] = [
            _field("Columns", [80, 132], initial.get("cols", 80)),
            _field("Rows", [24, 36, 48], initial.get("rows", 24)),
            _field("Font size", list(range(8, 33)), initial.get("font_size", 13)),
            _field("FPS", [15, 30, 60, 90, 120], initial.get("fps", 30)),
            _field(
                "Theme",
                ["amber", "dark", "light", "green"],
                initial.get("theme", "amber"),
            ),
            _field(
                "Line height",
                [0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 1.75, 2.0],
                initial.get("line_height", 1.0),
            ),
        ]
        # Readonly info fields
        self.fields.append(
            SetupField(
                "Server",
                [server_info.get("version", "vncvt")],
                readonly=True,
            )
        )
        self.fields.append(
            SetupField(
                "Clients",
                [str(server_info.get("clients", 0))],
                readonly=True,
            )
        )

        self.selected = 0  # index into self.fields (editable only)
        self._editable_count = 6  # first N fields are editable
        self.redraw()

    # -------- key handling --------

    def on_key(self, keysym: int) -> bool:
        """Handle a key event. Returns True if consumed."""
        if keysym == _KEY_UP:
            self.selected = (self.selected - 1) % self._editable_count
            self.redraw()
            return True
        if keysym == _KEY_DOWN:
            self.selected = (self.selected + 1) % self._editable_count
            self.redraw()
            return True
        if keysym in (_KEY_RETURN, _KEY_ENTER, _KEY_RIGHT):
            self.fields[self.selected].cycle(1)
            self.redraw()
            return True
        if keysym == _KEY_LEFT:
            self.fields[self.selected].cycle(-1)
            self.redraw()
            return True
        return False

    # -------- rendering --------

    def redraw(self) -> None:
        """Paint the overlay into self.screen via escape sequences."""
        lines: list[str] = []
        lines.append(self._separator("VNCVT SET-UP"))
        lines.append("")

        for i, field in enumerate(self.fields[:self._editable_count]):
            lines.append(self._format_field(field, selected=(i == self.selected)))

        lines.append("")
        lines.append(self._separator("Info"))
        for field in self.fields[self._editable_count:]:
            lines.append(self._format_field(field, selected=False))

        lines.append("")
        lines.append(self._separator())
        lines.append("  Arrows: navigate  Return: cycle  Esc: apply  F3: cancel")

        # Reset + clear screen, then feed each line
        self.stream.feed("\x1b[2J\x1b[H")  # clear + home
        for line in lines:
            self.stream.feed(line + "\r\n")
        self.screen.dirty.update(range(self.rows))

    def _separator(self, label: str = "") -> str:
        if label:
            inner = f" {label} "
            left = 3
            right = max(0, self.cols - left - len(inner) - 1)
            return "─" * left + inner + "─" * right
        return "─" * (self.cols - 1)

    def _format_field(self, field: SetupField, selected: bool) -> str:
        label = f"  {field.label:<14}: "
        value = f"[{field.current()}]"
        line = label + value
        if selected:
            # SGR 7 = reverse video, SGR 0 = reset
            return f"\x1b[7m{line:<{self.cols - 1}}\x1b[0m"
        return line

    # -------- snapshot --------

    def snapshot(self) -> dict[str, Any]:
        """Return the current editable field values by label."""
        return {
            field.label: field.current()
            for field in self.fields[:self._editable_count]
        }
