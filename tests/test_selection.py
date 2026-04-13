"""Drag-to-copy: PointerEvent selection round-trips as ServerCutText.

1. Take an initial screenshot so the asyncvnc Video state is primed.
2. Drive a drag (button-1 down -> move -> release) across row 0 of
   the framebuffer, which covers the bash prompt.
3. Read messages from the underlying reader (bypassing asyncvnc's
   broken UpdateType mapping — see tests/helpers.py) until we get
   a ServerCutText (msg type 3), then assert its content.
"""

from __future__ import annotations

import asyncio

from .helpers import read_one_message


async def test_drag_select_populates_clipboard(vnc):
    # Prime the video state and make sure we're in a quiescent frame.
    await vnc.screenshot()

    # Drag across the top row (which has the bash prompt, starting
    # with "root@..."). y=10 is inside cell row 0 after 5px padding.
    vnc.mouse.move(5, 10)
    with vnc.mouse.hold(button=0):       # left button (button index 0)
        vnc.mouse.move(300, 10)
        await vnc.drain()
    await vnc.drain()

    # Read messages until a ServerCutText arrives. Drain any framebuffer
    # updates along the way.
    cut_text: str | None = None
    deadline = asyncio.get_event_loop().time() + 3.0
    while cut_text is None and asyncio.get_event_loop().time() < deadline:
        try:
            msg_type, payload = await asyncio.wait_for(
                read_one_message(vnc), timeout=0.5
            )
        except asyncio.TimeoutError:
            continue
        if msg_type == 3:
            cut_text = payload.decode("latin-1")

    assert cut_text, "no ServerCutText received after drag"
    # The prompt always starts with "root@" in a default bash shell.
    assert any(marker in cut_text for marker in ("root@", "@", "#", "$")), (
        f"clipboard content doesn't look like a prompt row: {cut_text!r}"
    )
