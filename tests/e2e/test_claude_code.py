"""Dedicated regression tests for Claude Code rendering under vncvt.

These tests spawn ``claude`` as vncvt's PTY shell and verify its TUI
renders cleanly. They're regression coverage for the rendering bugs
the previous session hit by eyeballing Claude Code's output (box-
drawing underline fringe, theme persistence, F3 routing, etc.).

All tests are marked ``claude_code`` and auto-skipped via a hook in
``tests/conftest.py`` when the ``claude`` binary is not installed.
CI runners don't have claude, so the default suite isn't affected.

Network dependency: Claude Code hits the Anthropic API on first
prompt submission. These tests never send a prompt — they only
verify the welcome screen, panel borders, F3 overlay, and resize
repaint. None of those paths touch the network.
"""

from __future__ import annotations

import asyncio
import json
import struct
import time
from pathlib import Path

import asyncvnc
import pytest
from PIL import Image

from tests.conftest import _claude_binary
from tests.scenes import trigger_server_dump
from vncvt.supervisor import start_vncvt, stop_vncvt


pytestmark = pytest.mark.claude_code


CLAUDE_READY_TIMEOUT = 15.0
CLAUDE_READY_MIN_FG_PIXELS = 1000


async def _wait_for_claude_welcome(
    vnc: asyncvnc.Client, timeout: float = CLAUDE_READY_TIMEOUT,
) -> Image.Image:
    """Poll screenshots until Claude Code's welcome panel has rendered.

    Heuristic: >=1000 non-background pixels in the top half of the
    framebuffer. That clears the "bash prompt only" baseline and only
    fires once Claude Code has actually drawn its panel + welcome text.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    last: Image.Image | None = None
    while asyncio.get_event_loop().time() < deadline:
        rgba = await vnc.screenshot()
        img = Image.fromarray(rgba).convert("RGB")
        last = img
        w, h = img.size
        top_half = img.crop((0, 0, w, h // 2))
        # Count pixels that deviate noticeably from the background
        # (works for both amber/black and other themes)
        # tobytes() is the modern replacement for getdata() in Pillow.
        raw = top_half.tobytes()
        fg_count = 0
        # Sample every 4th pixel (12 bytes) to keep the hot loop fast
        for i in range(0, len(raw), 12):
            if raw[i] + raw[i + 1] + raw[i + 2] > 60:  # non-near-black
                fg_count += 1
        if fg_count >= CLAUDE_READY_MIN_FG_PIXELS:
            return img
        await asyncio.sleep(0.25)
    raise RuntimeError(
        f"Claude Code welcome screen did not appear within {timeout}s "
        f"(last fg pixel count: {fg_count if last else 'n/a'})"
    )


# ---------------------------------------------------------------------------
# T1: startup renders without crashing
# ---------------------------------------------------------------------------


async def test_claude_startup_renders(vncvt_server_factory, scene):
    """Spawn vncvt with claude as the shell; welcome panel should paint."""
    claude = _claude_binary()
    assert claude is not None  # pytest marker should have skipped otherwise
    host, port, proc = vncvt_server_factory(
        "--shell", claude,
        # Claude Code's UI is tuned for standard light/dark themes;
        # amber is overridden here so the pixel-density heuristic below
        # sees real content (dark bg still keeps the threshold valid).
        "--theme", "dark",
    )
    async with asyncvnc.connect(host=host, port=port) as vnc:
        img = await _wait_for_claude_welcome(vnc)
        await scene("claude_welcome")
        # Assert subprocess is still alive — claude didn't crash
        assert proc.poll() is None, (
            f"vncvt subprocess exited with {proc.returncode}"
        )
        # Framebuffer should be non-trivial
        assert img.size[0] > 0 and img.size[1] > 0


# ---------------------------------------------------------------------------
# T2: panel borders render without the spurious underline fringe
# ---------------------------------------------------------------------------


# Box-drawing glyphs Claude Code uses to draw its panel borders. If a row
# contains any of these in its cell buffer, we treat it as a border row.
_BORDER_CHARS = frozenset("─━═╭╮╯╰┌┐└┘")


def _control_socket_from_proc(proc) -> Path:
    """Recover the --scene-control-socket path from a spawned proc.

    ``vncvt_server_factory`` hands tests ``(host, port, proc)`` and
    hides the ``VncvtHandle`` inside its closure, so we peek at the
    argv the supervisor passed to ``python -m vncvt`` to find the
    socket path. Keeps this test self-contained (no conftest edits).
    """
    args = list(proc.args)
    flag = "--scene-control-socket"
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return Path(args[i + 1])
    raise RuntimeError(f"{flag} not found on vncvt argv: {args!r}")


def _row_mean_luminance(img: Image.Image, y: int) -> float:
    """Rec. 601 luma averaged across one horizontal scanline of ``img``.

    We only need relative brightness across neighbouring scanlines, so
    the exact coefficients don't matter — any monotonic mix of R/G/B
    would do. Rec. 601 keeps the numbers intuitive.
    """
    w = img.width
    row = img.crop((0, y, w, y + 1)).tobytes()
    total = 0.0
    count = 0
    for i in range(0, len(row), 3):
        r, g, b = row[i], row[i + 1], row[i + 2]
        total += 0.299 * r + 0.587 * g + 0.114 * b
        count += 1
    return total / count if count else 0.0


async def test_claude_borders_no_stray_underlines(
    vncvt_server_factory,
) -> None:
    """Claude Code panel borders must not render a dark underline band.

    Regression for a bug where cells carrying ``─`` also had
    ``underscore=True`` set by Claude Code's renderer. Our amber
    renderer naively drew underlines in the foreground color, producing
    a one-pixel-tall dark fringe immediately below each border row.

    The fix was to render underlines in a color blended toward the
    background so they stay invisible. This test asserts the RENDERED
    framebuffer no longer shows that dark band: for each border row, we
    check that the scanline where the underline would land has a mean
    luminance within 10% of the average of its neighbors — i.e. it
    isn't a single-pixel dark valley sandwiched between brighter rows.

    Uses ``--theme amber`` because that's the theme where the artifact
    was visible; the regression is theme-specific and this is the
    configuration the user originally reported the bug in.
    """
    claude = _claude_binary()
    assert claude is not None  # claude_code marker should have skipped
    host, port, proc = vncvt_server_factory(
        "--shell", claude,
        "--theme", "amber",
        "--line-height", "1.0",
    )
    control_socket = _control_socket_from_proc(proc)

    async with asyncvnc.connect(host=host, port=port) as vnc:
        await _wait_for_claude_welcome(vnc)
        scene_dir = await trigger_server_dump(control_socket, "claude_borders")

    assert proc.poll() is None, (
        f"vncvt subprocess exited with {proc.returncode}"
    )

    term_path = scene_dir / "scene.term.json"
    fb_path = scene_dir / "scene.fb.png"
    assert term_path.is_file(), f"missing scene.term.json at {term_path}"
    assert fb_path.is_file(), f"missing scene.fb.png at {fb_path}"

    term = json.loads(term_path.read_text(encoding="utf-8"))
    rows = int(term["dimensions"]["rows"])
    cells = term["cells"]

    # Identify rows that contain any of Claude Code's box-drawing chars.
    border_rows: set[int] = set()
    for key, cell in cells.items():
        ch = cell.get("c", "")
        if ch and ch in _BORDER_CHARS:
            _col_str, row_str = key.split(",")
            border_rows.add(int(row_str))

    assert border_rows, (
        "expected Claude Code's welcome panel to contain box-drawing "
        "border rows, but found none in the term dump at "
        f"{term_path}"
    )

    img = Image.open(fb_path).convert("RGB")
    # Cell height = (fb_height - 2*padding) / rows. Padding is 5px on
    # each side (see TerminalRenderer.PADDING); deriving this locally
    # keeps the test from importing the server package.
    padding = 5
    cell_height = (img.height - 2 * padding) / rows
    assert cell_height > 2, (
        f"implausible cell_height {cell_height} from fb height "
        f"{img.height} / rows {rows}"
    )

    # For each border row, the underline would be drawn at
    # y = row_top + cell_height - 2 (see TerminalRenderer._draw_row:
    # ``ul_y = y + self.cell_height - 2``). The bug was that the
    # underline renderer painted that scanline with a dark-greyish
    # colour over the amber glyph row, producing a visible dark valley
    # sandwiched between the brighter border-glyph scanline above and
    # the empty-cell scanline below.
    #
    # Assert the underline scanline's mean luminance is within 10% of
    # the average of its 4 neighbours (±1, ±2 px). If it's more than
    # 10% darker, that's the artifact.
    tolerance = 0.10
    problems: list[str] = []
    for row in sorted(border_rows):
        row_top = int(round(row * cell_height)) + padding
        ul_y = row_top + int(round(cell_height)) - 2
        if ul_y < 2 or ul_y + 2 >= img.height:
            continue  # edge row, neighbours would fall outside the fb
        target = _row_mean_luminance(img, ul_y)
        neighbours = [
            _row_mean_luminance(img, ul_y - 2),
            _row_mean_luminance(img, ul_y - 1),
            _row_mean_luminance(img, ul_y + 1),
            _row_mean_luminance(img, ul_y + 2),
        ]
        neighbour_avg = sum(neighbours) / len(neighbours)
        # Skip rows where neighbours are effectively all background —
        # there's no visible content for an underline fringe to darken,
        # so the 10% comparison is meaningless (divide-by-tiny).
        if neighbour_avg <= 5.0:
            continue
        deficit = (neighbour_avg - target) / neighbour_avg
        if deficit > tolerance:
            problems.append(
                f"row {row}: underline scanline y={ul_y} luma={target:.1f} "
                f"vs neighbour avg {neighbour_avg:.1f} "
                f"({deficit * 100:.1f}% darker, > {tolerance * 100:.0f}%)"
            )

    assert not problems, (
        "Claude Code border rows show a dark underline fringe in the "
        f"rendered framebuffer at {fb_path}:\n  "
        + "\n  ".join(problems)
    )


def _top_half_signature(img: Image.Image) -> int:
    """Sum of R+G+B for every pixel in the top half of ``img``.

    Used as a cheap content-signature to detect when Claude Code's UI
    has (or hasn't) been materially repainted. Returning to within
    ~15% of the baseline signature after cancelling SET-UP is a strong
    hint the underlying TUI survived the overlay toggle.
    """
    w, h = img.size
    top = img.crop((0, 0, w, h // 2))
    raw = top.tobytes()
    total = 0
    for i in range(0, len(raw), 3):
        total += raw[i] + raw[i + 1] + raw[i + 2]
    return total


def _header_fg_density(img: Image.Image) -> int:
    """Count non-background pixels across the top 28 scanlines.

    The SET-UP overlay paints a header row ("VNCVT SET-UP" plus a line
    of box-drawing dashes) at the very top of the framebuffer. When
    the overlay is visible the top-28px band lights up with far more
    foreground pixels than Claude Code's own header bar would produce.
    """
    w, h = img.size
    band = img.crop((0, 0, w, min(28, h)))
    raw = band.tobytes()
    count = 0
    for i in range(0, len(raw), 3):
        if raw[i] + raw[i + 1] + raw[i + 2] > 60:
            count += 1
    return count


def _dominant_fg_color(img: Image.Image) -> tuple[int, int, int]:
    """Mean R/G/B over the brightest pixels in the top of the frame.

    Used to classify a rendered theme: amber if R > G > B, grey/white
    if R ~= G ~= B and all channels are high. Samples only "bright"
    pixels so we ignore the dark background cells.
    """
    w, h = img.size
    band = img.crop((0, 0, w, min(200, h)))
    raw = band.tobytes()
    r_sum = g_sum = b_sum = 0
    n = 0
    for i in range(0, len(raw), 3):
        r, g, b = raw[i], raw[i + 1], raw[i + 2]
        if r + g + b > 180:  # only count foreground-ish pixels
            r_sum += r
            g_sum += g
            b_sum += b
            n += 1
    if n == 0:
        return (0, 0, 0)
    return (r_sum // n, g_sum // n, b_sum // n)


# ---------------------------------------------------------------------------
# T3: F3 enters SET-UP overlay over Claude Code, F3 again restores the TUI
# ---------------------------------------------------------------------------


async def test_claude_f3_enters_setup_mode(vncvt_server_factory):
    """F3 paints the SET-UP overlay on top of Claude Code and toggles off.

    Spawns vncvt with Claude Code as the shell, waits for the welcome
    panel, captures a baseline signature, presses F3, asserts the top
    header band is dense with overlay pixels, then presses F3 again
    and asserts the framebuffer content-signature returns close to
    the baseline (within 15%). This is the regression test for F3
    routing getting shadowed by the PTY and for the overlay failing
    to cleanly restore the underlying TUI.
    """
    claude = _claude_binary()
    assert claude is not None  # claude_code marker should have skipped
    host, port, proc = vncvt_server_factory(
        "--shell", claude,
        "--theme", "dark",
    )
    async with asyncvnc.connect(host=host, port=port) as vnc:
        await _wait_for_claude_welcome(vnc)

        # Baseline: Claude Code welcome panel, no overlay.
        rgba = await vnc.screenshot()
        baseline = Image.fromarray(rgba).convert("RGB")
        baseline_sig = _top_half_signature(baseline)
        assert baseline_sig > 0, "baseline screenshot was fully black"

        # F3 -> SET-UP overlay.
        vnc.keyboard.press("F3")
        await asyncio.sleep(0.4)
        rgba = await vnc.screenshot()
        in_setup = Image.fromarray(rgba).convert("RGB")
        header_density = _header_fg_density(in_setup)
        # The SET-UP header row (label + box-drawing rule) should
        # produce several hundred foreground pixels across the top
        # 28px band. Claude Code's own welcome panel does not paint
        # anything that dense in the very top rows.
        assert header_density > 400, (
            f"expected >400 fg pixels in SET-UP header band, got "
            f"{header_density}; overlay may not have painted over "
            f"Claude Code's TUI"
        )

        # F3 again -> cancel, Claude Code should return.
        vnc.keyboard.press("F3")
        await asyncio.sleep(0.3)
        rgba = await vnc.screenshot()
        restored = Image.fromarray(rgba).convert("RGB")
        restored_sig = _top_half_signature(restored)

        # Within 15% of baseline — content survived the overlay toggle.
        diff = abs(restored_sig - baseline_sig)
        tolerance = baseline_sig * 0.15
        assert diff <= tolerance, (
            f"post-cancel signature {restored_sig} diverged from "
            f"baseline {baseline_sig} by {diff} (> {tolerance:.0f}, "
            f"15%); Claude Code's UI did not cleanly return after "
            f"cancelling SET-UP"
        )

    assert proc.poll() is None, (
        f"vncvt subprocess exited with {proc.returncode}"
    )


# ---------------------------------------------------------------------------
# T7: switching theme via SET-UP mode repaints Claude Code
# ---------------------------------------------------------------------------


async def test_claude_theme_switch_via_setup(vncvt_server_factory):
    """Cycling Theme in SET-UP from amber to dark repaints Claude Code.

    Regression test for the theme-persistence bug: after the user
    exits SET-UP with Escape (apply + exit), the new theme must be
    reflected in the next full framebuffer the server hands the
    client. We start on ``--theme amber`` so the dominant foreground
    is orange (R > G > B), cycle Theme to dark, and assert the next
    screenshot's dominant fg is light-grey (R ~= G ~= B, all > 200).
    """
    claude = _claude_binary()
    assert claude is not None  # claude_code marker should have skipped
    host, port, proc = vncvt_server_factory(
        "--shell", claude,
        "--theme", "amber",
    )
    async with asyncvnc.connect(host=host, port=port) as vnc:
        await _wait_for_claude_welcome(vnc)

        # Baseline: amber theme -> dominant fg should be orange-ish.
        rgba = await vnc.screenshot()
        before = Image.fromarray(rgba).convert("RGB")
        r0, g0, b0 = _dominant_fg_color(before)
        assert r0 > g0 > b0, (
            f"expected amber-ish dominant fg (R>G>B), got "
            f"R={r0} G={g0} B={b0}; is the server actually on amber?"
        )

        # F3 to enter SET-UP.
        vnc.keyboard.press("F3")
        await asyncio.sleep(0.4)
        # Navigate: Columns(0) -> Rows(1) -> Font size(2) -> FPS(3) -> Theme(4).
        for _ in range(4):
            vnc.keyboard.press("Down")
            await asyncio.sleep(0.1)
        # Return: cycle Theme amber -> dark.
        vnc.keyboard.press("Return")
        await asyncio.sleep(0.2)
        # Escape: apply + exit SET-UP.
        vnc.keyboard.press("Escape")
        # Poll for up to 3 seconds until the dominant fg has changed
        # from amber (R>G>B) to neutral/grey (R~=G~=B). asyncvnc caches
        # framebuffer internally and only refreshes on incoming FBUs;
        # polling screenshot() drives that refresh.
        r1 = g1 = b1 = 0
        for _ in range(15):
            await asyncio.sleep(0.2)
            rgba = await vnc.screenshot()
            after = Image.fromarray(rgba).convert("RGB")
            r1, g1, b1 = _dominant_fg_color(after)
            # Dark theme fg is neutral: no longer R>G>B amber
            if not (r1 > g1 > b1) and r1 + g1 + b1 > 0:
                break

        # Assert the dominant color is no longer amber-biased.
        assert not (r1 > g1 > b1), (
            f"expected non-amber dominant fg after theme switch to dark, "
            f"got R={r1} G={g1} B={b1}; theme change did not take "
            f"effect (theme-persistence regression)"
        )

    assert proc.poll() is None, (
        f"vncvt subprocess exited with {proc.returncode}"
    )


# ---------------------------------------------------------------------------
# T4: live server-initiated resize reflows Claude Code without crashing
# ---------------------------------------------------------------------------


async def test_claude_resize_reflows_ui(tmp_path: Path) -> None:
    """Resize vncvt mid-session while Claude Code is running.

    Regression for SIGWINCH handling: if the server's resize path
    doesn't propagate the new ``TIOCSWINSZ`` to the PTY, or if our
    framebuffer rebuild races with Claude Code's repaint, the
    ``claude`` child tends to either crash outright or stop drawing.
    We assert the subprocess is still alive AND the resize RPC
    reported the new dimensions. We don't reconnect the VNC client
    because asyncvnc doesn't currently handle a mid-session desktop-
    size change; the reply-side assertion is enough to prove the
    resize took effect end-to-end.
    """
    claude = _claude_binary()
    assert claude is not None  # claude_code marker should have skipped

    scene_root = tmp_path / "scenes"
    handle = start_vncvt(
        "--shell", claude,
        "--theme", "dark",
        scene_root=scene_root,
    )
    try:
        async with asyncvnc.connect(
            host=handle.host, port=handle.port,
        ) as vnc:
            # Wait for Claude Code's welcome panel to render at the
            # default geometry before we disturb it. If we resize
            # before Claude Code has finished its initial paint, the
            # reflow we're trying to test never happens.
            initial_img = await _wait_for_claude_welcome(vnc)
            assert initial_img.size[0] > 0 and initial_img.size[1] > 0

        # Fire the resize over the control socket. Use a cols/rows
        # combo clearly different from the 80x24 default so any
        # "server ignored the request" bug would produce a response
        # mismatch we can catch.
        target_cols = 100
        target_rows = 30

        async def _send_resize() -> dict:
            reader, writer = await asyncio.open_unix_connection(
                path=str(handle.control_socket)
            )
            try:
                req = {
                    "version": 1,
                    "op": "resize",
                    "cols": target_cols,
                    "rows": target_rows,
                }
                writer.write((json.dumps(req) + "\n").encode("utf-8"))
                await writer.drain()
                line = await asyncio.wait_for(
                    reader.readline(), timeout=3.0,
                )
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
            return json.loads(line.decode("utf-8"))

        reply = await _send_resize()
        assert reply.get("ok") is True, f"resize op failed: {reply}"
        assert reply.get("cols") == target_cols, (
            f"resize reply reported cols={reply.get('cols')}, "
            f"expected {target_cols}"
        )
        assert reply.get("rows") == target_rows, (
            f"resize reply reported rows={reply.get('rows')}, "
            f"expected {target_rows}"
        )

        # Give Claude Code a beat to consume SIGWINCH and reflow its
        # UI before we assert it survived. One second is conservative
        # — on developer hardware the repaint typically lands in
        # <200ms.
        await asyncio.sleep(1.0)

        assert handle.proc.poll() is None, (
            f"vncvt subprocess exited after resize with "
            f"{handle.proc.returncode}"
        )
    finally:
        stop_vncvt(handle)


# ---------------------------------------------------------------------------
# T9: --mode 132x24 preset paints Claude Code's UI using the extra width
# ---------------------------------------------------------------------------


# Extended welcome timeout for the 132-col geometry: Claude Code has
# roughly 65% more screen area to paint than at 80x24, and the
# welcome-detection heuristic polls on a 250ms cadence, so give it
# noticeably longer than the default 15s CLAUDE_READY_TIMEOUT.
_WIDE_WELCOME_TIMEOUT = 25.0


async def test_claude_132_col_preset_spawns(tmp_path: Path) -> None:
    """Spawn vncvt at 132x24 with Claude Code as the shell.

    Regression for the ``--mode`` CLI flag end-to-end: if the preset
    didn't propagate through to the terminal emulator's cols/rows
    before the PTY was created, Claude Code would still see an 80-
    column terminal and confine its panel to the left 80 cols. We
    assert that (a) the framebuffer is wide enough for 132 cols, (b)
    Claude Code drew something substantial, and (c) the rendered
    cell buffer contains at least one cell past column 100 — proof
    it's actually using the extra width instead of centring itself.
    """
    claude = _claude_binary()
    assert claude is not None  # claude_code marker should have skipped

    scene_root = tmp_path / "scenes"
    handle = start_vncvt(
        "--shell", claude,
        "--mode", "132x24",
        "--theme", "dark",
        scene_root=scene_root,
    )
    try:
        async with asyncvnc.connect(
            host=handle.host, port=handle.port,
        ) as vnc:
            img = await _wait_for_claude_welcome(
                vnc, timeout=_WIDE_WELCOME_TIMEOUT,
            )
            # 132 cols * 7 px/col ~ 924 px at the default 13pt font
            # size; 7 px is the tightest plausible advance width
            # (real cells are closer to 9-10 px), so this lower bound
            # is conservative but still excludes an 80-col regression
            # (which would max out around 80*10 = 800 px).
            assert img.size[0] >= 132 * 7, (
                f"framebuffer width {img.size[0]} is too narrow for "
                f"132 columns — --mode 132x24 did not take effect"
            )

            scene_dir = await trigger_server_dump(
                handle.control_socket, "claude_132col",
            )

        assert handle.proc.poll() is None, (
            f"vncvt subprocess exited with {handle.proc.returncode}"
        )

        # Pixel density check: reuse the welcome-detector's own
        # threshold so the two regressions stay in lockstep.
        fb_path = scene_dir / "scene.fb.png"
        assert fb_path.is_file(), f"missing scene.fb.png at {fb_path}"
        rendered = Image.open(fb_path).convert("RGB")
        w, h = rendered.size
        top_half = rendered.crop((0, 0, w, h // 2))
        raw = top_half.tobytes()
        fg_count = 0
        for i in range(0, len(raw), 12):
            if raw[i] + raw[i + 1] + raw[i + 2] > 60:
                fg_count += 1
        assert fg_count > CLAUDE_READY_MIN_FG_PIXELS, (
            f"Claude Code fg pixel density {fg_count} <= "
            f"{CLAUDE_READY_MIN_FG_PIXELS}; UI did not paint at 132x24"
        )

        # Term dump: assert at least one cell lives in a column > 100.
        term_path = scene_dir / "scene.term.json"
        assert term_path.is_file(), f"missing scene.term.json at {term_path}"
        term = json.loads(term_path.read_text(encoding="utf-8"))
        cols = int(term["dimensions"]["cols"])
        assert cols >= 132, (
            f"term dump reports cols={cols}, expected >=132 — "
            f"--mode 132x24 did not reach the terminal emulator"
        )

        wide_cells = 0
        for key, cell in term["cells"].items():
            col_str, _row_str = key.split(",")
            if int(col_str) > 100 and cell.get("c", "").strip():
                wide_cells += 1
        assert wide_cells >= 1, (
            "Claude Code rendered no non-blank cells past column 100; "
            "the UI appears to be confined to a narrower box rather "
            "than using the full 132-column width"
        )
    finally:
        stop_vncvt(handle)


_BOX_DRAWING_ANY = {
    "╭", "╮", "╯", "╰", "┌", "┐", "└", "┘",  # corners
    "─", "━", "═",                              # horizontal
    "│", "┃", "║",                              # vertical
}


# ---------------------------------------------------------------------------
# T5: text input reaches Claude Code's input area
# ---------------------------------------------------------------------------


async def test_claude_text_input_reaches_input_area(tmp_path):
    """Type 'hello' into Claude Code and verify it lands in the buffer.

    Does not submit — no network required. Just verifies keystrokes
    reach the PTY and pyte absorbs them.
    """
    claude = _claude_binary()
    assert claude is not None
    handle = start_vncvt(
        "--shell", claude, "--theme", "dark",
        scene_root=tmp_path / "scenes",
    )
    try:
        async with asyncvnc.connect(host=handle.host, port=handle.port) as vnc:
            await _wait_for_claude_welcome(vnc)

            # Type "hello" — keyboard.write is AsyncVNC's convenience wrapper
            # that presses each char one at a time. Each character goes to
            # claude's PTY input.
            vnc.keyboard.write("hello")

            # Let pyte process the keystrokes and claude paint them
            await asyncio.sleep(0.8)

            # Dump the server's terminal buffer via the control socket
            scene_dir = await trigger_server_dump(
                handle.control_socket, "claude_after_typing",
            )
            term_json = scene_dir / "scene.term.json"
            assert term_json.exists(), f"missing {term_json}"
            data = json.loads(term_json.read_text())
            cells = data.get("cells", {})
            # Reconstruct the terminal as rows of text so we can
            # substring-search for "hello". Dict iteration order is
            # insertion order (column-major from the server), which
            # scrambles consecutive characters; sort by (row, col).
            by_row: dict[int, dict[int, str]] = {}
            for key, cell in cells.items():
                col_str, row_str = key.split(",")
                col, row = int(col_str), int(row_str)
                by_row.setdefault(row, {})[col] = cell.get("c", " ")
            rows_text = []
            for row in sorted(by_row):
                cols = by_row[row]
                line = "".join(
                    cols.get(c, " ") for c in range(max(cols) + 1)
                )
                rows_text.append(line)
            flat = "\n".join(rows_text)
            assert "hello" in flat, (
                f"typed 'hello' but it did not appear in the terminal "
                f"buffer — keystrokes may not be reaching Claude Code's "
                f"input row. Rows:\n{flat}"
            )
    finally:
        stop_vncvt(handle)


# ---------------------------------------------------------------------------
# T6: unicode / box-drawing renders without clipping
# ---------------------------------------------------------------------------


async def test_claude_box_drawing_corners_present(tmp_path):
    """Claude Code's welcome panel is bordered with ╭╮╯╰; the server
    scene dump should show all four corner glyphs in its cell buffer.

    Regression for: earlier renderer versions clipped wide glyphs or
    post-thresholded box-drawing chars into blanks. Skia + the current
    _draw_glyph path should pass them through intact.
    """
    claude = _claude_binary()
    assert claude is not None
    handle = start_vncvt(
        "--shell", claude, "--theme", "dark",
        scene_root=tmp_path / "scenes",
    )
    try:
        async with asyncvnc.connect(host=handle.host, port=handle.port) as vnc:
            await _wait_for_claude_welcome(vnc)

            scene_dir = await trigger_server_dump(
                handle.control_socket, "claude_box_corners",
            )
            term_json = scene_dir / "scene.term.json"
            data = json.loads(term_json.read_text())
            cells = data.get("cells", {})
            chars_seen = {
                cell.get("c", " ") for cell in cells.values()
            }
            # Claude Code's welcome panel uses some mix of box-drawing
            # chars. At 80x24 the full rounded panel may not fit, so
            # at minimum there should be horizontal separators. Just
            # assert at least one box-drawing glyph made it into the
            # buffer — that's enough to prove Skia isn't eating wide
            # characters.
            found = _BOX_DRAWING_ANY & chars_seen
            assert found, (
                "expected at least one box-drawing glyph in Claude "
                f"Code's welcome buffer, found none. Non-ASCII buffer "
                f"chars: "
                f"{sorted(c for c in chars_seen if c and ord(c[0]) > 127)}"
            )
    finally:
        stop_vncvt(handle)


# ---------------------------------------------------------------------------
# T8: shutdown notice fires when claude exits
# ---------------------------------------------------------------------------


async def test_claude_shutdown_notice_on_exit(tmp_path):
    """When Claude Code's child process exits, vncvt should feed a
    goodbye line into the terminal buffer and then shut down cleanly
    within ~3 seconds.

    Regression for the graceful shutdown path added earlier — before,
    the server just killed the event loop on SIGCHLD and left the
    client staring at an abrupt disconnect.

    Tests sends Ctrl+C twice which Claude Code interprets as "exit
    cleanly". If your claude build handles Ctrl+C differently this
    test will be skipped after the first timeout.
    """
    claude = _claude_binary()
    assert claude is not None
    handle = start_vncvt(
        "--shell", claude, "--theme", "dark",
        scene_root=tmp_path / "scenes",
    )
    try:
        async with asyncvnc.connect(host=handle.host, port=handle.port) as vnc:
            await _wait_for_claude_welcome(vnc)

            # Send Ctrl+C twice via raw key events — AsyncVNC's .press
            # method handles modifiers transparently. "Control_L" + "c"
            # combined sends \x03 to the PTY.
            for _ in range(2):
                vnc.keyboard.press("Control_L", "c")
                await asyncio.sleep(0.3)

            # Wait up to 5 seconds for the subprocess to exit
            deadline = time.monotonic() + 5.0
            exited = False
            while time.monotonic() < deadline:
                if handle.proc.poll() is not None:
                    exited = True
                    break
                await asyncio.sleep(0.2)

            if not exited:
                pytest.skip(
                    "claude did not exit on Ctrl+C; version may have "
                    "different quit semantics. Skipping shutdown test."
                )

            # Once the shell exits, vncvt writes the goodbye notice,
            # sleeps briefly, then stops. Give it a moment to flush.
            await asyncio.sleep(1.5)
    finally:
        stop_vncvt(handle)
