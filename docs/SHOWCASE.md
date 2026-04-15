# Theme showcase

Five built-in themes, each with an OKLCH-generated 16-color ANSI palette at uniform perceived lightness. All clear WCAG AAA at peak contrast; body-text modal contrast clears AA on every theme.

All screenshots capture Claude Code's welcome panel at 80×24, SF Mono 13pt, `line-height 1.1`, `contrast max`, `color-mode 16-color` (the runtime defaults).

## `amber` — warm amber on black *(default)*

![amber](screenshots/claude-code-amber.png)

| Metric | Value |
|---|---|
| Background | `#000000` |
| Body text | `(255, 190, 80)` warm amber |
| Modal contrast | **9.91:1** |
| Peak contrast | 12.73:1 |
| Palette style | Hand-tuned `_AMBER_ANSI` — every ANSI slot is a variant of warm amber |

The primary aesthetic. Monotone by design: the OKLCH generator would pick canonical red/green/blue which defeats the "everything is amber phosphor" intent, so the palette is hand-tuned into a single-hue family.

## `green` — phosphor green on black

![green](screenshots/claude-code-green.png)

| Metric | Value |
|---|---|
| Background | `#000000` |
| Body text | `(120, 255, 120)` phosphor green |
| Modal contrast | **12.10:1** |
| Peak contrast | 16.37:1 |
| Palette style | Hand-tuned `_GREEN_ANSI` — every ANSI slot is a phosphor-green variant |

The "other" VT220-era aesthetic. Same single-hue design as amber. Classic IBM 3270 / DEC VT52 / Apple //e green-screen look.

## `light` — black on white

![light](screenshots/claude-code-light.png)

| Metric | Value |
|---|---|
| Background | `#ffffff` |
| Body text | `#000000` |
| Modal contrast | **15.91:1** |
| Peak contrast | 21.00:1 (WCAG maximum) |
| Palette style | OKLCH `base_l=0.40, bright_l=0.28, chroma=0.17` — dark colors on white |

Generated, not hand-tuned, but at low lightness (`base_l=0.40`) so dark-red / dark-blue / dark-green all perceptually collapse into "dark something" against the white bg. Looks near-monotone for the same reason amber and green do, despite having real chroma in every slot.

## `dark` — white on black

![dark](screenshots/claude-code-dark.png)

| Metric | Value |
|---|---|
| Background | `#000000` |
| Body text | `#ffffff` |
| Modal contrast | **15.46:1** |
| Peak contrast | 20.12:1 |
| Palette style | OKLCH `base_l=0.78, bright_l=0.92, chroma=0.17` — bright chromatic hues on black |

The "modern terminal" theme. Unlike amber/green/light, this one's generated at high lightness, so every ANSI slot pops as its own hue. More xterm than VT220.

## `powershell` — off-white on navy `#012456`

![powershell](screenshots/claude-code-powershell.png)

| Metric | Value |
|---|---|
| Background | `#012456` (Windows PowerShell classic navy) |
| Body text | `#EEEDF0` off-white |
| Modal contrast | **11.00:1** |
| Peak contrast | 12.97:1 |
| Palette style | OKLCH `base_l=0.78, bright_l=0.92, chroma=0.17` — same params as `dark`, different bg |

Shares the OKLCH parameters with `dark`, so the 16 ANSI hues are identical in RGB — only the bg color differs. On navy, Claude Code's ANSI 1 (red) borders read as a striking warm-red; on `dark` the same slot blends into the black bg.

## Palette taxonomy

| Theme | Family | ANSI palette source |
|---|---|---|
| `amber` | Hand-tuned single-hue (phosphor aesthetic) | `_AMBER_ANSI` dict |
| `green` | Hand-tuned single-hue (phosphor aesthetic) | `_GREEN_ANSI` dict |
| `light` | OKLCH dark-on-light, low lightness reads as monotone | `_gen_palette(_LIGHT_BG, base_l=0.40, …)` |
| `dark` | OKLCH bright-on-dark, full-spectrum chromatic | `_gen_palette(_DARK_BG, base_l=0.78, …)` |
| `powershell` | OKLCH bright-on-navy, full-spectrum chromatic | `_gen_palette(_NAVY_BG, base_l=0.78, …)` |

There are effectively **two design families**:

- **Phosphor themes** (`amber`, `green`, `light`) — single-hue or near-monotone. Faithful to the VT220 / Apple //e / newspaper-print aesthetic.
- **Chromatic themes** (`dark`, `powershell`) — full-spectrum xterm-style palettes. More colors, more modern.

Definitions live in [`vncvt/renderer.py` lines 25–140](../vncvt/renderer.py). The OKLCH generator itself is in [`vncvt/oklch.py`](../vncvt/oklch.py).

## Contrast measurement

- **Modal** = WCAG 2.1 ratio between the two most common colors in the rendered screenshot (usually bg vs body text — the dominant visual contrast the eye settles on).
- **Peak** = maximum fg/bg ratio the palette can produce — the best-case for any cell.

Both are measured on the actual rendered PNG, not computed from palette hex values, so they reflect the Skia AA + LCD filter + `contrast max` embolden exactly as the user sees them. The measurement script is [`scripts/measure_contrast.py`](../scripts/measure_contrast.py).

## Regenerating the screenshots

```bash
uv run python scripts/refresh_showcase.py
```

This spawns vncvt once per theme, drives Claude Code's welcome panel through a temporary profile, dumps the framebuffer via the scene-control socket, and saves both a full-height archival PNG (`claude-code-init-<theme>.png`) and a cropped version (`claude-code-<theme>.png`) used by this document.

Captures are timing-sensitive because Claude's welcome panel paints asynchronously — if a theme screenshot looks wrong, re-run just that theme:

```python
import asyncio
from scripts.refresh_showcase import capture_theme
asyncio.run(capture_theme('powershell'))
```
