# Design: color in vncvt

Why every color in vncvt is what it is.

## Goals

1. **Readable at all times**. Every rendering must clear WCAG 2.1 AA
   (4.5:1) for body text and AAA (7:1) where the theme allows. No
   cell should ever become invisible or illegible because of a
   color-math shortcut.
2. **Historically coherent**. The user's North Star is an amber DEC
   VT220. Every theme and every mode anchors to a specific display
   era (MDA, CGA, EGA, VGA, WinXP truecolor) so the mental model is
   concrete instead of "vague color knob".
3. **Perceptually calibrated**. Uniform *perceived* brightness
   across hues, not uniform sRGB values. Every palette is generated
   or tuned in OKLCH, not RGB.
4. **TUIs-aware**. Modern tools like Claude Code emit a mix of ANSI
   16, 256, and 24-bit truecolor. vncvt must have a deliberate
   strategy for each of those three escape families.

## Two orthogonal dimensions

Color in vncvt is controlled by **two independent knobs**:

1. **`--theme`** — picks the aesthetic family: what color the
   phosphor glows in, what bg the "paper" is. 5 options:
   `amber`, `green`, `light`, `dark`, `powershell`.
2. **`--color-mode`** — picks the fidelity tier: how much of the
   TUI's intended color palette makes it through. 4 options:
   `phosphor`, `16-color`, `256-color`, `true-color`.

A theme defines *bg* + *fg* + *hue family*. A mode defines *how
ANSI and truecolor inputs are mapped onto that hue family*. Every
(theme, mode) pair is a valid rendering.

## Themes

Five themes, each anchored to a specific physical or historical
display:

| Theme | Reference | Background | Primary fg | Hue family |
|---|---|---|---|---|
| `amber` | DEC VT220 amber phosphor (1983) | `(0, 0, 0)` black | `(255, 190, 80)` warm amber | Single-hue amber ramp |
| `green` | IBM 3270 / DEC VT52 phosphor (1972+) | `(0, 0, 0)` black | `(120, 255, 120)` phosphor green | Single-hue green ramp |
| `light` | 1980s white-on-paper emulation | `(255, 255, 255)` white | `(0, 0, 0)` black | Dark-grey ramp |
| `dark` | Generic modern terminal on black | `(0, 0, 0)` black | `(255, 255, 255)` white | Light-grey ramp |
| `powershell` | Windows PowerShell classic | `(1, 36, 86)` navy | `(238, 237, 240)` off-white | Cyan/blue ramp |

The bg/fg poles are WCAG-verified:

| Theme | Modal contrast | Peak contrast | WCAG level |
|---|---|---|---|
| `amber` | 9.91:1 | 12.73:1 | AAA |
| `green` | 12.10:1 | 16.37:1 | AAA |
| `light` | 15.91:1 | 21.00:1 | AAA (max) |
| `dark` | 15.46:1 | 20.12:1 | AAA |
| `powershell` | 11.00:1 | 12.97:1 | AAA |

**Modal** = WCAG between the two most-used colors in the rendered
screenshot. **Peak** = maximum the palette can produce.

## Color modes

Four fidelity tiers, each anchored to a PC display era:

### `phosphor` (default) — VT220 / MDA / Hercules (1981–83)

Single-hue, 16 intensity shades of the theme's primary hue. Every
theme gets a monochrome rendering faithful to 1980s hardware
terminals.

**How it works**: ANSI inputs look up a per-theme `_<THEME>_PHOSPHOR`
16-slot dict. Each dict has all 16 SGR slots (red, green, yellow,
blue, magenta, cyan, white, plus brights) but every entry is a
variant of the theme's single hue — amber red is warm amber, amber
green is warm amber, amber blue is warm amber. The SGR index
becomes a *brightness cue*, not a hue cue. Truecolor hex inputs
collapse through the theme's bg→fg OKLab ramp, which is
single-hue by construction.

**Amber phosphor palette** (hand-tuned, preserved from the
original vncvt release):

```python
_AMBER_PHOSPHOR = {
    "black":         (60, 40, 0),
    "red":           (255, 120, 60),
    "green":         (220, 200, 80),
    "brown":         (255, 220, 80),
    "blue":          (230, 180, 60),
    ...
}
```

All entries have r > g > b (the warmth invariant). Every hue in
Claude Code's welcome panel — red borders, green title, white
text — collapses into the warm amber family.

### `16-color` — CGA / EGA (1981 / 1984)

16 distinct hues (red, green, blue, yellow, magenta, cyan, plus
brights). Theme-tinted but still multi-hue. Each theme has a
`_<THEME>_CGA` palette that respects SGR hue identity (red is
red-ish) while biasing the overall chroma toward the theme's bg.

**Why "theme-tinted"**: a pure CGA red on the amber theme's black
bg would be visually jarring — the whole point of the amber
aesthetic is warmth. So the amber CGA palette shifts all hues
slightly warm: reds are fire-red, greens are olive, blues are
teal. Still 16 recognizably distinct hues, but with a family
resemblance to the theme.

**Generation**: `vncvt/oklch.py::generate_palette(bg, base_l,
bright_l, chroma, hue_bias)`. The `hue_bias` parameter rotates
each hue slot by up to 30° toward the theme's primary hue.

### `256-color` — VGA Mode 13h (1987)

256-slot palette with more gradation than `16-color` but less
chroma than `true-color`. The 256 slots come from:

- Slots 0–15 — the theme's CGA 16 palette (same as `16-color` mode)
- Slots 16–231 — xterm 6×6×6 RGB cube, each entry **desaturated
  40% toward the theme bg/fg midpoint**
- Slots 232–255 — 24-step greyscale, interpolated along the
  theme's bg→fg OKLab axis

The desaturation is what "diminished chroma" means. A pure blue
input (`48;5;21` = `#0000ff`) in `true-color` mode renders as
pure blue; in `256-color` mode it renders as a muted slate-blue
that belongs on the current theme's palette. You see the blueness
intended by the TUI, but the navy/amber/green theme personality
still comes through.

### `true-color` — Win95 True Color dropdown / ISO 8613-6 (1995 / 2012+)

Raw passthrough. ANSI inputs look up the **standard xterm
256-color palette** (fixed, theme-independent). Truecolor hex
inputs render as raw RGB for backgrounds; foregrounds still go
through the OKLab readability lift so dim-grey text stays legible.

**This is the only mode where Claude Code's native palette
renders faithfully.** The amber/green/powershell *themes* still
affect the default bg (you see Claude on an amber or navy
backdrop) but Claude's ANSI indices and truecolor escapes pass
through untouched.

## Mode × theme matrix

Twenty combinations. Here's what each produces conceptually:

| | `phosphor` | `16-color` | `256-color` | `true-color` |
|---|---|---|---|---|
| `amber` | warm amber shades | warm-biased 16 hues | warm-biased 256 cube | native ANSI + raw bg |
| `green` | phosphor green shades | green-biased 16 hues | green-biased 256 cube | native ANSI + raw bg |
| `light` | dark grey on white | dark CGA on white | dark 256 on white | native ANSI + raw bg |
| `dark` | white grey on black | bright CGA on black | bright 256 on black | native ANSI + raw bg |
| `powershell` | cyan/blue on navy | cyan-biased CGA on navy | cyan-biased 256 on navy | native ANSI + raw bg |

Defaults: `amber` + `phosphor`. That's a VT220 out of the box.

## Why OKLCH

sRGB luminance (Rec-601 weights) is perceptually wrong: two
colors with equal sRGB Y can look dramatically different in
brightness to the human eye. This matters a lot for palette
generation because we want all 16 ANSI slots to feel equally
bright, not to have RGB values that happen to match.

OKLab (2020, [Björn Ottosson](https://bottosson.github.io/posts/oklab/))
is a perceptually uniform color space — one L* unit delta produces
the same perceived brightness change regardless of hue. OKLCH
(the polar-coordinate form of OKLab, analogous to HSL vs HSV for
sRGB) lets us pick colors by *lightness + chroma + hue* and
trust that changing any one axis doesn't accidentally warp the
others.

vncvt uses OKLCH in three places:

1. **Palette generation** (`vncvt/oklch.py`). Each CGA/VGA palette
   slot is picked at a constant L* target with constrained chroma,
   then converted to sRGB. Uniform perceived lightness is the
   primary invariant.
2. **fg readability lift** (`vncvt/renderer.py::_apply_oklab_ramp`).
   Truecolor hex inputs for fg are remapped along the theme's
   bg→fg OKLab axis with a gamma 0.4 lift so dim grey (`#808080`)
   becomes readable on any theme bg. The lift is cell-bg-aware:
   if a TUI emits a custom bg (like Claude Code's `#eeeeee` status
   bar) the fg gets remapped relative to *that* bg, not the theme's
   default bg.
3. **256-color chroma reduction**. The diminished palette blends
   each cube entry toward the theme's OKLab midpoint at 40% to
   mute saturation without shifting hue.

## Why two phosphor families (hand-tuned vs generated)

The amber and green phosphor palettes are **hand-tuned dicts**,
not OKLCH generator output. The other three themes' phosphor
palettes are also hand-tuned but with a systematic approach.

The reason amber and green are special: the OKLCH generator picks
*canonical* red/green/blue/cyan/magenta/yellow hues. On amber or
green themes, those canonical hues *defeat the aesthetic* — a
pure red on an amber theme isn't amber, it's red. The whole point
of the phosphor mode is that *everything* is amber. Hand-tuning
lets us say "SGR 1 (red) renders as warm amber, SGR 2 (green)
renders as slightly different warm amber" — preserving SGR slot
identity while violating SGR slot *color* identity on purpose.

The `light`, `dark`, and `powershell` phosphor palettes could
technically be generated (greyscale ramp for light/dark, cyan
ramp for powershell) but we hand-tune them for the same reason:
the specific greys and cyans should match the theme's overall
aesthetic, and the tuning is cheap enough to do once and commit.

## Why `phosphor` is the default

Historical accuracy. A user running `vncvt` with no flags should
see the closest software approximation of an amber DEC VT220 the
project can produce. That's:

- Theme: `amber` (warm orange phosphor on black)
- Mode: `phosphor` (single-hue, 16 shades of intensity)
- Contrast: `max` (thick strokes compensate for AA gamma on
  non-retina displays, matching the "painted glass" feel of CRT
  phosphor)

Any other default is a compromise. Users who want modern Claude
Code rendering switch with `--color-mode true-color` — they'll
get the amber backdrop but native Claude hues on top.

## Why the bg passes through raw in `true-color` but fg doesn't

Two different concerns:

- **bg passthrough** preserves the TUI's intended backdrop. Claude
  Code's `#eeeeee` status bar is meaningful — it separates UI
  chrome from body text. Remapping it destroys that information.
- **fg lift** preserves readability. A TUI that emits
  `#808080` dim grey is fine on a white terminal but invisible on
  black. Lifting it through the OKLab ramp picks the nearest
  readable grey on the current theme's bg.

So we pass bg through untouched (preserves TUI intent) and lift
fg (preserves readability). The two goals don't conflict because
bg and fg live on different sides of the contrast equation.

## Why contrast max is default

On a non-retina display, Skia's LCD filter + subpixel AA renders
thin strokes with gamma-correct partial coverage — which, on a
96 DPI screen, reads as "slightly dim" because the eye integrates
AA edges into perceived intensity. Thick strokes compensate: a
2-pixel stem with full coverage reads brighter than a 1-pixel
stem with AA partial coverage, even though the sRGB integrals
match.

The user runs a non-retina display. Contrast `max` is a direct
response to that hardware reality, not a generic preference.
(`contrast normal` is fine on retina; `contrast high` is the
middle ground.)

## Open questions

- **256-color chroma reduction curve**: should the desaturation be
  linear (40% blend toward midpoint) or perceptual (OKLab chroma
  reduction)? Linear is simpler and fast; OKLab is more correct.
  Currently the plan uses linear; revisit if 256-color screenshots
  look wrong.
- **Per-theme phosphor tuning**: `light`, `dark`, `powershell`
  phosphor palettes are hand-tuned at implementation time. If any
  theme's phosphor mode looks off, retune and document here.
- **Flag for TUI app authors**: a future flag could expose the
  active color mode to the child PTY via `$COLORTERM` or a custom
  `$VNCVT_COLOR_MODE` env var, so TUIs can adapt their own palette
  choices.

## References

- Björn Ottosson, [*"A perceptual color space for image processing"*](https://bottosson.github.io/posts/oklab/) — OKLab definition, 2020
- [WCAG 2.1 contrast ratio](https://www.w3.org/TR/WCAG21/#contrast-minimum) — the 4.5:1 / 7:1 thresholds
- [DEC VT220 Programmer Reference](https://vt100.net/docs/vt220-rm/) — SGR slot conventions
- [Xterm 256-color palette](https://jonasjacek.github.io/colors/) — the true-color mode lookup table
- [ISO 8613-6 SGR 38;2/48;2](https://en.wikipedia.org/wiki/ANSI_escape_code#SGR_(Select_Graphic_Rendition)_parameters) — truecolor escape standard
- See also [docs/SHOWCASE.md](SHOWCASE.md) for per-theme screenshots and measured values.
