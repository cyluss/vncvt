"""Smoke test: connect via AsyncVNC and take a screenshot."""

from __future__ import annotations

import imagehash
import pytest
from PIL import Image

from vncvt.renderer import TerminalRenderer

from tests.baselines import assert_hash


# Font size the CLI uses by default — see ``vncvt/__main__.py``'s
# ``--font-size`` argparse default. Kept in sync manually; if the CLI
# default changes again the assertion below will fail loudly with the
# expected vs. actual dimensions, pointing straight at this constant.
_CLI_DEFAULT_FONT_SIZE = 13


async def test_connects_and_screenshots(vnc, scene):
    """AsyncVNC client connects to a freshly spawned vncvt and gets
    a non-empty framebuffer back, sized to the renderer's output for
    the CLI's default 80x24 grid at the default font size."""
    rgba = await vnc.screenshot()  # numpy HxWx4
    assert rgba is not None
    h, w, c = rgba.shape
    assert c == 4
    expected = TerminalRenderer(
        cols=80, rows=24, font_size=_CLI_DEFAULT_FONT_SIZE
    )
    assert (w, h) == (expected.width, expected.height), (
        f"expected {expected.width}x{expected.height} (renderer at "
        f"font_size={_CLI_DEFAULT_FONT_SIZE}), got {w}x{h}"
    )
    await scene("initial_prompt")


async def test_initial_framebuffer_matches_baseline(vnc, scene, update_baselines):
    """Perceptual-hash regression: the initial prompt render should
    stay visually stable across commits. dHash tolerance 8 absorbs
    normal font-rendering noise; anything larger signals a real
    cosmetic change that needs a baseline refresh."""
    await scene("initial_prompt_dhash")
    rgba = await vnc.screenshot()
    img = Image.fromarray(rgba).convert("RGB")
    h = imagehash.dhash(img, hash_size=16)
    assert_hash("handshake_initial_prompt", h, tolerance=8, update=update_baselines)
