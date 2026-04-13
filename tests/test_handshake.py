"""Smoke test: connect via AsyncVNC and take a screenshot."""

from __future__ import annotations

import imagehash
import pytest
from PIL import Image

from .baselines import assert_hash


async def test_connects_and_screenshots(vnc, scene):
    """AsyncVNC client connects to a freshly spawned vncvt and gets
    a non-empty framebuffer back."""
    rgba = await vnc.screenshot()  # numpy HxWx4
    assert rgba is not None
    h, w, c = rgba.shape
    assert c == 4
    # Default vncvt framebuffer is 80 cols * 10 px + 2*5 padding = 810
    # and 24 rows * 19 px + 2*5 padding = 466.
    assert (w, h) == (810, 466), f"expected 810x466, got {w}x{h}"
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
