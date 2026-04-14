"""Clipboard paste: client sends ClientCutText, server writes to PTY.

Verifies the full round trip:
1. Paste a marker string via `Clipboard.write` (AsyncVNC wraps
   ClientCutText).
2. Press Enter to execute the pasted command.
3. Screenshot and assert the echoed marker is visible by counting
   amber text pixels in the top rows.
"""

from __future__ import annotations

import asyncio

from PIL import Image

from .helpers import client_cut_text


MARKER = "PASTE_FEATURE_D"


async def test_paste_renders_as_amber_text(vnc, scene):
    await scene("before_paste")
    # Send "echo MARKER" via clipboard paste, then press Enter.
    client_cut_text(vnc, f"echo {MARKER}")
    vnc.keyboard.press("Return")
    # Let bash echo, pyte absorb, and the update loop render.
    await asyncio.sleep(1.0)
    await scene("after_paste_enter")

    rgba = await vnc.screenshot()
    img = Image.fromarray(rgba).convert("RGB")
    w, _ = img.size

    # Scan the top 8 rows (at 19px cell height, plus 5px padding)
    # for amber text pixels. The echoed command line + its output
    # should contribute several hundred amber pixels.
    top = 5
    bottom = 5 + 8 * 19
    amber_pixels = 0
    for y in range(top, bottom):
        for x in range(5, w - 5):
            r, g, b = img.getpixel((x, y))
            if r > 200 and 100 < g < 200 and b < 80:
                amber_pixels += 1
    # Threshold of 250 comfortably clears the ~320 amber pixels
    # produced by two lines of echoed text + the prompt at the
    # default 11pt font. Was 500 before we disabled the zsh session
    # banner, whose UUID text inflated the top-row pixel count.
    assert amber_pixels > 250, (
        f"expected >250 amber pixels in top band, got {amber_pixels}"
    )
