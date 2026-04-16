# Design: color in vncvt

Why every color in vncvt is what it is.

## Goals

1. **Readable at all times**. Every rendering must clear WCAG 2.1 AA
   (4.5:1) for body text and AAA (7:1) where the theme allows. No
   cell should ever become invisible or illegible because of a
   color-math shortcut.
2. **Historically coherent**. Each theme anchors to a specific physical
   display or era — a phosphor coating, a hardware chip's analog
   output, a piece of software so ubiquitous its color scheme became
   cultural memory. The mental model is concrete, not "vague color
   knob".
3. **Perceptually calibrated**. Uniform *perceived* brightness across
   hues, not uniform sRGB values. Every palette is generated or tuned
   in OKLCH, not RGB.
4. **TUIs-aware**. Modern tools like Claude Code emit a mix of ANSI 16,
   256, and 24-bit truecolor. `phosphor` collapses all of that to a
   single hue; `true-color` passes it through. The choice is explicit
   and binary.

## Two dimensions

Color in vncvt is controlled by **two independent knobs**:

1. **`--theme`** — the aesthetic family: which historical display you
   are simulating. 7 options: `amber`, `green`, `dark`, `light`,
   `c64`, `dos`, `atari`.
2. **`--color-mode`** — the fidelity tier: `phosphor` (single-hue,
   default) or `true-color` (native passthrough).

A theme defines *bg + fg + hue family*. A mode defines *how ANSI and
truecolor inputs are mapped onto that family*. Every (theme, mode) pair
is a valid rendering.

## Themes

Seven themes in two historical categories.

### Phosphor CRT types

These simulate monochrome CRT monitors. The phosphor coating determined
the glow color; there was no other color information. `phosphor` mode
is their natural home.

| Theme | Reference | BG | FG | Bold |
|---|---|---|---|---|
| `amber` | DEC VT220 amber phosphor (1983) | `(0,0,0)` | `(255,190,80)` | `(255,220,120)` |
| `green` | IBM 3270 / DEC VT52 P31 phosphor (1972+) | `(0,0,0)` | `(120,255,120)` | `(180,255,180)` |
| `dark` | White phosphor CRT — P4 coating (PDP-11 era) | `(0,0,0)` | `(255,255,255)` | `(255,255,255)` |
| `light` | Paper-white display — original Mac (1984) | `(255,255,255)` | `(0,0,0)` | `(0,0,0)` |

### 8-bit / early PC color schemes

These simulate specific hardware or software color identities from the
8-bit and early PC era. They were color displays; `true-color` mode is
their natural home, though `phosphor` mode collapses them to their fg
hue for the retro effect.

| Theme | Reference | BG | FG | Bold |
|---|---|---|---|---|
| `c64` | Commodore 64 BASIC screen — Pepto NTSC palette | `(53,40,121)` | `(112,109,235)` | `(170,170,255)` |
| `dos` | CGA dark blue — WordPerfect 5.1 / Norton Commander | `(0,0,170)` | `(255,255,255)` | `(255,255,85)` |
| `atari` | Atari 8-bit GTIA chip — register $2C hue 2 lum 6 | `(0,0,0)` | `(253,193,112)` | `(255,220,160)` |

The `dos` bold accent `(255,255,85)` = CGA bright yellow — the color
every DOS productivity app used for emphasis (active menus, selections,
highlighted text).

The `atari` fg `(253,193,112)` is register `$2C` from the GTIA's hue-2
("red-orange") family at luminance 6, sourced from the Lospec
atari-8-bit-family-gtia palette. It is the brightest warm orange before
the hue tips into peach at `$2E`.

The `c64` fg `(112,109,235)` is Pepto's light-blue (color 14). Against
the dark-blue bg `(53,40,121)` the raw Pepto contrast is ~3:1 — below
WCAG AA. The `bold` slot `(170,170,255)` clears 4.9:1 and is used as
the default fg in practice; see *C64 contrast note* below.

### Contrast table

| Theme | fg contrast | bold contrast | WCAG level |
|---|---|---|---|
| `amber` | 12.73:1 | ~14.5:1 | AAA |
| `green` | 16.37:1 | ~20:1 | AAA |
| `dark` | 21.00:1 | 21.00:1 | AAA (max) |
| `light` | 21.00:1 | 21.00:1 | AAA (max) |
| `c64` (bold as fg) | 4.94:1 | 4.94:1 | AA |
| `dos` | 16.94:1 | ~13:1 | AAA |
| `atari` | 13.40:1 | ~15:1 | AAA |

**C64 contrast note**: the authentic Pepto light-blue (`#706DEB`) falls
below AA against the Commodore dark-blue (`#352879`). On a real 1702
monitor the CRT bloom and phosphor persistence inflated perceived
contrast; on a modern LCD it reads too dim. vncvt uses the `bold` value
as the effective default fg, keeping the C64 blue-on-blue palette
family while clearing WCAG AA.

## Color modes

### `phosphor` (default) — VT220 / MDA / Hercules (1981–83)

Single-hue. Every ANSI index and every truecolor hex input collapses to
a brightness variant of the theme's primary hue. "Claude Code" hyperlink
blue → amber. Syntax-highlight red → amber. The SGR index becomes a
*brightness cue*, not a hue cue.

**How it works**: ANSI inputs look up a per-theme `_<THEME>_PHOSPHOR`
16-slot dict. Every entry in the dict is a variant of the theme's hue
— amber red is warm amber, amber green is warm amber, amber blue is warm
amber. Truecolor hex inputs collapse through the theme's bg→fg OKLab
ramp, which is single-hue by construction.

**Amber phosphor palette** (hand-tuned, preserved from the original
vncvt release):

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

All entries satisfy r ≥ g ≥ b (the warmth invariant). Every hue in
Claude Code's welcome panel — red borders, green title, white text —
collapses into the warm amber family.

### `true-color` — ISO 8613-6 / Win95 (1995 / 2012+)

Raw passthrough. ANSI inputs look up the **standard xterm 256-color
palette** (fixed, theme-independent). Truecolor hex inputs render as
raw RGB for backgrounds; foregrounds still go through the OKLab
readability lift so dim text stays legible.

**This is the only mode where Claude Code's native palette renders
faithfully.** The theme still affects the default bg (you see Claude on
an amber or navy backdrop) but Claude's ANSI indices and truecolor
escapes pass through untouched.

The 8-bit themes (`c64`, `dos`, `atari`) are most natural in
`true-color`: the bg/fg match their historical color identity, and
any application running inside picks up standard ANSI colors from xterm.

## Mode × theme matrix

Fourteen combinations, all intentional:

| | `phosphor` | `true-color` |
|---|---|---|
| `amber` | warm amber shades only | native ANSI + amber bg |
| `green` | green shades only | native ANSI + green bg |
| `dark` | grey shades only | native ANSI + black bg |
| `light` | dark-ink shades only | native ANSI + white bg |
| `c64` | blue-purple shades only | native ANSI + Commodore blue bg |
| `dos` | cool-white shades only | native ANSI + CGA dark-blue bg |
| `atari` | warm-orange shades only | native ANSI + black bg, orange fg |

Defaults: `amber` + `phosphor`. That's a VT220 out of the box.

## Why these themes and not others

**Why not `powershell`**: the PowerShell color scheme (`#012456` navy,
`#EEEDF0` off-white) is a 2006 Microsoft UI design decision, not a
physical display type. It has no phosphor, no chip, no hardware
reference. Removed.

**Why `dos` instead of specific DOS apps**: all major DOS productivity
apps — WordPerfect 5.1, Norton Commander, Turbo C, QBasic — used the
same underlying CGA hardware palette and all chose dark blue (`#0000AA`)
as their primary background. The theme captures the era, not one app.

**Why `c64` and not VIC-20**: the VIC-20 had identical default colors
(same Commodore blue palette). `c64` covers both.

**Why `atari` fg from GTIA hue 2**: the GTIA chip's hue-2 ("red-orange")
family at high luminance is the warm orange-gold that appears throughout
Atari 8-bit games and demos — the color most associated with the Atari
aesthetic. The default BASIC screen used grey text on black, which is
covered by `dark`; the GTIA warm orange is what makes Atari visually
distinct.

**Why not 16-color or 256-color modes**: these intermediate tiers
(theme-tinted 16 hues; diminished xterm cube) were scope creep. No
user reaches for `--color-mode 256-color` with conscious intent. The
real choice is binary: *"do you want aesthetic uniformity (phosphor) or
do you want the app's real colors (true-color)?"*

## Why OKLCH

sRGB luminance (Rec-601 weights) is perceptually wrong: two colors with
equal sRGB Y can look dramatically different in brightness to the human
eye. OKLab (2020, Björn Ottosson) is perceptually uniform — one L*
unit delta produces the same perceived brightness change regardless of
hue.

vncvt uses OKLCH in two places:

1. **Phosphor palette generation** (`vncvt/oklch.py`). Each phosphor
   slot is picked at a constant L* target with constrained chroma, then
   converted to sRGB. Uniform perceived lightness is the primary
   invariant.
2. **fg readability lift** (`vncvt/renderer.py::_apply_oklab_ramp`).
   Truecolor hex fg inputs are remapped along the theme's bg→fg OKLab
   axis with a gamma 0.4 lift so dim grey (`#808080`) becomes readable
   on any theme bg. The lift is cell-bg-aware: if a TUI emits a custom
   bg (like Claude Code's `#eeeeee` status bar) the fg is remapped
   relative to *that* bg, not the theme's default bg.

## Why `phosphor` is the default

Historical accuracy. A user running `vncvt` with no flags should see
the closest software approximation of an amber DEC VT220. That is:

- Theme: `amber` (warm orange phosphor on black)
- Mode: `phosphor` (single-hue, 16 shades of intensity)

Any other default is a compromise. Users who want Claude Code's native
colors switch with `--color-mode true-color` — they keep the amber
backdrop but get Claude's actual hues on top.

## Why bg passes through raw in `true-color` but fg doesn't

Two different concerns:

- **bg passthrough** preserves the TUI's intended backdrop. Claude
  Code's `#eeeeee` status bar is meaningful — it separates UI chrome
  from body text. Remapping it destroys that information.
- **fg lift** preserves readability. A TUI that emits `#808080` dim
  grey is fine on a white terminal but invisible on black. Lifting it
  through the OKLab ramp picks the nearest readable grey on the current
  theme's bg.

bg and fg live on different sides of the contrast equation, so
the two strategies don't conflict.

## Open questions

- **Atari phosphor palette tuning**: the `_ATARI_PHOSPHOR` 16-slot dict
  needs hand-tuning. Starting point: interpolate the GTIA hue-2 ramp
  ($20→$2F) and map all 16 ANSI slots to that ramp by luminance.
- **C64 phosphor palette**: similarly needs a blue-purple single-hue
  ramp using the Pepto blue family.
- **Flag for TUI app authors**: a future flag could expose the active
  color mode to the child PTY via `$COLORTERM` or a custom
  `$VNCVT_COLOR_MODE` env var, so TUIs can adapt their own palette
  choices.

## References

- Björn Ottosson, [*"A perceptual color space for image processing"*](https://bottosson.github.io/posts/oklab/) — OKLab definition, 2020
- [WCAG 2.1 contrast ratio](https://www.w3.org/TR/WCAG21/#contrast-minimum) — the 4.5:1 / 7:1 thresholds
- [DEC VT220 Programmer Reference](https://vt100.net/docs/vt220-rm/) — SGR slot conventions
- [Xterm 256-color palette](https://jonasjacek.github.io/colors/) — the true-color mode lookup table
- [ISO 8613-6 SGR 38;2/48;2](https://en.wikipedia.org/wiki/ANSI_escape_code#SGR_(Select_Graphic_Rendition)_parameters) — truecolor escape standard
- [Pepto's Commodore 64 NTSC palette](https://www.pepto.de/projects/colorvic/) — C64 reference colors
- [Lospec atari-8-bit-family-gtia](https://lospec.com/palette-list/atari-8-bit-family-gtia) — GTIA palette hex values
- [Atari GTIA color registers](https://www.atariarchives.org/mapping/appendix5.php) — hue/luminance register map
- See also [docs/SHOWCASE.md](SHOWCASE.md) for per-theme screenshots and measured values.
