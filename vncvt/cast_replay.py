"""Replay asciinema v2 .cast files through vncvt's renderer and
inspect the resulting framebuffer.

This is the library side of ``scripts/replay_cast.py``. Use it
directly from tests, notebooks, or other tooling that wants to
feed PTY bytes into pyte + ``TerminalRenderer`` and measure what
came out.

Typical usage::

    from vncvt.cast_replay import replay_cast, inspect_frame

    frame = replay_cast(
        "session.cast", theme="light", at="end",
    )
    report = inspect_frame(frame, threshold=4.5)
    for cell in report.below_threshold:
        print(cell)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import pyte
from PIL import Image

from .renderer import TerminalRenderer, apply_theme


# ---------- cast file parsing ----------


@dataclass
class CastEvent:
    t: float
    kind: str  # "o" | "i" | "r"
    data: str


@dataclass
class Cast:
    header: dict
    events: list[CastEvent]

    @property
    def cols(self) -> int:
        return int(self.header.get("width", 80))

    @property
    def rows(self) -> int:
        return int(self.header.get("height", 24))

    @property
    def duration(self) -> float:
        return self.events[-1].t if self.events else 0.0


def load_cast(path: Path | str) -> Cast:
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise ValueError(f"empty .cast: {path}")
    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed .cast header in {path}: {exc}") from exc
    if not isinstance(header, dict):
        raise ValueError(
            f"malformed .cast header in {path}: expected object, got {type(header).__name__}"
        )
    events: list[CastEvent] = []
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        ev = json.loads(line)
        events.append(CastEvent(t=float(ev[0]), kind=ev[1], data=ev[2]))
    return Cast(header=header, events=events)


# ---------- replay ----------


@dataclass
class ReplayedFrame:
    """One rendered moment from a cast replay."""

    cast_path: Path
    theme: str
    at: float
    image: Image.Image
    screen: "pyte.Screen"

    def save_png(self, out_path: Path | str) -> Path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        self.image.convert("RGB").save(out_path)
        return out_path


def replay_cast(
    cast_path: Path | str,
    theme: str = "light",
    at: float | str = "end",
    font_size: int = 13,
    line_height: float = 1.1,
    contrast: str = "high",
    color_mode: str = "phosphor",
) -> ReplayedFrame:
    """Replay ``cast_path`` up to timestamp ``at`` and render one
    framebuffer PNG. ``at`` may be a float (seconds) or ``"end"``.
    ``color_mode`` selects the rendering fidelity tier:
    ``"phosphor"`` (default, VT220 single-hue), ``"16-color"``
    (CGA/EGA multi-hue, theme-tinted), ``"256-color"`` (VGA diminished
    chroma, theme-tinted), or ``"true-color"`` (native xterm + raw RGB
    passthrough)."""
    cast = load_cast(cast_path)
    apply_theme(theme)
    rend = TerminalRenderer(
        cols=cast.cols, rows=cast.rows,
        font_size=font_size, line_height=line_height, contrast=contrast,
        color_mode=color_mode,
    )
    screen = pyte.Screen(cast.cols, cast.rows)
    stream = pyte.Stream(screen)

    cutoff = float("inf") if at == "end" else float(at)
    for ev in cast.events:
        if ev.t > cutoff:
            break
        if ev.kind == "o":
            stream.feed(ev.data)
        elif ev.kind == "r":
            cols, rows = (int(x) for x in ev.data.split("x"))
            screen.resize(rows, cols)

    img = rend.full_render(screen)
    return ReplayedFrame(
        cast_path=Path(cast_path), theme=theme,
        at=cutoff if cutoff != float("inf") else cast.duration,
        image=img, screen=screen,
    )


def replay_frames(
    cast_path: Path | str,
    timestamps: list[float],
    theme: str = "light",
    **kwargs,
) -> Iterator[ReplayedFrame]:
    """Yield one ``ReplayedFrame`` per timestamp. Re-replays from the
    start each time — O(n*m) but trivially correct. Sufficient for
    small-to-medium casts; optimize later if needed."""
    for ts in timestamps:
        yield replay_cast(cast_path, theme=theme, at=ts, **kwargs)


# ---------- inspection ----------


@dataclass
class CellContrast:
    col: int
    row: int
    char: str
    fg_hex: str
    bg_hex: str
    ratio: float


@dataclass
class ContrastReport:
    count: int
    min: float
    p10: float
    median: float
    p90: float
    max: float
    below_threshold: list[CellContrast] = field(default_factory=list)
    threshold: float = 4.5

    def __bool__(self) -> bool:
        return self.count > 0

    def format(self) -> str:
        lines = [
            f"Cell contrast stats ({self.count} non-space cells):",
            f"  min    = {self.min:6.2f}:1",
            f"  p10    = {self.p10:6.2f}:1",
            f"  median = {self.median:6.2f}:1",
            f"  p90    = {self.p90:6.2f}:1",
            f"  max    = {self.max:6.2f}:1",
            "",
        ]
        if not self.below_threshold:
            lines.append("All cells clear the contrast threshold.")
        else:
            lines.append(f"{len(self.below_threshold)} cells below threshold:")
            lines.append(
                f"  {'col':>4} {'row':>4}  {'char':6}  "
                f"{'fg':8}  {'bg':8}  {'ratio':>8}"
            )
            for c in sorted(self.below_threshold, key=lambda c: c.ratio):
                lines.append(
                    f"  {c.col:>4} {c.row:>4}  {c.char!r:6}  "
                    f"{c.fg_hex}  {c.bg_hex}  {c.ratio:>7.2f}:1"
                )
        return "\n".join(lines)


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(rgb: tuple[int, int, int]) -> float:
    return (
        0.2126 * _srgb_to_linear(rgb[0] / 255.0)
        + 0.7152 * _srgb_to_linear(rgb[1] / 255.0)
        + 0.0722 * _srgb_to_linear(rgb[2] / 255.0)
    )


def _wcag(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    l1, l2 = _luminance(fg), _luminance(bg)
    return (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)


def inspect_pixels(
    image: "Image.Image",
    cells: dict[tuple[int, int], str],
    cols: int,
    rows: int,
    threshold: float = 4.5,
) -> ContrastReport:
    """Run the per-cell WCAG walk against an arbitrary framebuffer.

    ``image`` is a Pillow image of the rendered framebuffer at vncvt's
    standard padding (5px). ``cells`` maps ``(col, row)`` to the
    character at that cell — only entries with a non-whitespace
    character are inspected. ``cols``/``rows`` are the terminal
    dimensions used to compute cell extents.

    This is the shared kernel behind both :func:`inspect_frame` (which
    walks a live ``pyte.Screen``) and the scene-bundle inspector in
    ``scripts/inspect_cell_contrast.py`` (which walks a serialized
    ``scene.term.json``)."""
    img = image.convert("RGB")

    padding = 5  # TerminalRenderer.PADDING
    cell_w = (img.width - 2 * padding) / cols
    cell_h = (img.height - 2 * padding) / rows

    below: list[CellContrast] = []
    ratios: list[float] = []
    for (col, row), ch in cells.items():
        if not ch or ch in (" ", "\xa0"):
            continue
        if not (0 <= col < cols and 0 <= row < rows):
            continue
        x0 = int(round(col * cell_w)) + padding
        y0 = int(round(row * cell_h)) + padding
        x1 = int(round((col + 1) * cell_w)) + padding
        y1 = int(round((row + 1) * cell_h)) + padding
        crop = img.crop((x0, y0, x1, y1))
        raw = crop.tobytes()
        bg = (raw[0], raw[1], raw[2])
        bg_lum = _luminance(bg)
        best = bg
        best_d = 0.0
        for i in range(0, len(raw), 3):
            px = (raw[i], raw[i + 1], raw[i + 2])
            d = abs(_luminance(px) - bg_lum)
            if d > best_d:
                best_d = d
                best = px
        ratio = _wcag(best, bg)
        ratios.append(ratio)
        if ratio < threshold:
            below.append(CellContrast(
                col=col, row=row, char=ch,
                fg_hex="#{:02x}{:02x}{:02x}".format(*best),
                bg_hex="#{:02x}{:02x}{:02x}".format(*bg),
                ratio=ratio,
            ))
    ratios.sort()
    if not ratios:
        return ContrastReport(
            count=0, min=0, p10=0, median=0, p90=0, max=0,
            below_threshold=[], threshold=threshold,
        )
    return ContrastReport(
        count=len(ratios),
        min=ratios[0],
        p10=ratios[len(ratios) // 10],
        median=ratios[len(ratios) // 2],
        p90=ratios[len(ratios) * 9 // 10],
        max=ratios[-1],
        below_threshold=below,
        threshold=threshold,
    )


def inspect_frame(
    frame: ReplayedFrame, threshold: float = 4.5,
) -> ContrastReport:
    """Walk every non-space cell in ``frame`` and compute the rendered
    WCAG contrast between its darkest-delta pixel and its corner (bg).
    Returns a ``ContrastReport`` with stats plus the list of cells
    below ``threshold``."""
    screen = frame.screen
    cols, rows = screen.columns, screen.lines
    cells: dict[tuple[int, int], str] = {}
    for row in range(rows):
        buf = screen.buffer[row]
        for col in range(cols):
            cells[(col, row)] = buf[col].data
    return inspect_pixels(
        frame.image, cells, cols, rows, threshold=threshold,
    )
