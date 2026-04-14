"""5px overscan padding: the border pixels must always be DEFAULT_BG."""

from __future__ import annotations

from PIL import Image

DEFAULT_BG = (0, 0, 0)
PADDING = 5


async def test_five_pixel_overscan_border(vnc):
    """All four corners, plus the pixel one-in from each corner,
    should be the dark-amber background. If anyone accidentally
    removes the padding or changes DEFAULT_BG without updating it,
    this fails loudly."""
    rgba = await vnc.screenshot()
    img = Image.fromarray(rgba).convert("RGB")
    w, h = img.size

    probes = [
        (0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1),
        (PADDING - 1, PADDING - 1),
        (w - PADDING, h - PADDING),
        # Middle of each border edge
        (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2),
    ]
    for (x, y) in probes:
        pixel = img.getpixel((x, y))
        assert pixel == DEFAULT_BG, (
            f"padding pixel ({x},{y}) = {pixel}, expected {DEFAULT_BG}"
        )
