# vncvt architecture

Internal reference for contributors and future Claude Code sessions.
Covers module layout, dependency graph, shared mutable state, the font
fallback chain, color pipeline, and test suite structure.

## Module dependency graph

Arrows point from importer to imported. Lazy (runtime) imports are
marked with `(lazy)`.

```
__main__.py
  |-- cast_recorder    (leaf)
  |-- terminal         (leaf)
  |-- renderer
  |     '-- oklch      (lazy, leaf)
  |-- server
  |     |-- terminal   (leaf)
  |     |-- renderer
  |     '-- setup_screen (lazy, leaf)
  '-- scene_dump
        |-- terminal   (TYPE_CHECKING only)
        '-- renderer   (TYPE_CHECKING only)

cast_replay.py
  '-- renderer

supervisor.py           (leaf -- zero vncvt imports)
```

**Leaf modules** (zero intra-package imports at runtime):
`terminal.py`, `oklch.py`, `setup_screen.py`, `supervisor.py`,
`cast_recorder.py`.

`scene_dump.py` imports `Terminal` and `TerminalRenderer` only under
`TYPE_CHECKING`, so at runtime it is also dependency-free within the
package.

## Module responsibilities

### `__main__.py` (352 lines)

CLI entry point. Parses arguments (argparse), loads TOML config from
`~/.config/vncvt/config.toml`, resolves `--mode` presets, calls
`apply_theme()`, constructs `Terminal`, `TerminalRenderer`, and
`RFBServer`, wires up the optional `CastRecorder` and `SceneDumper`,
installs a `SIGCHLD` handler for shell-exit detection, and runs the
asyncio event loop.

### `server.py` (1045 lines)

RFB protocol implementation over asyncio. `RFBServer` manages the TCP
listener, the PTY read callback, and a periodic `_update_loop` that
renders dirty rows and pushes framebuffer updates to connected clients.
`RFBClient` handles per-connection handshake (RFB 3.3/3.7/3.8), VNC
Authentication (DES challenge-response), `SetPixelFormat`,
`SetEncodings`, `KeyEvent` (with Ctrl/Alt tracking), `PointerEvent`
(drag-to-select), `ClientCutText`, and `SetDesktopSize`. Also hosts
the SET-UP mode enter/exit/apply lifecycle and the resize broadcast.

### `renderer.py` (1008 lines)

Pixel rendering engine. `TerminalRenderer` converts a `pyte.Screen`
into an RGBX PIL `Image` via Skia offscreen surfaces. Contains the
seven built-in phosphor palettes (amber, green, dark, light, c64, dos,
atari), the `THEMES` registry, and `apply_theme()` which mutates six
module-level globals. Implements the font fallback chain, cell-level
glyph rasterization with configurable contrast embolden
(normal/high/max), the `_resolve_color()` color pipeline, and the
OKLab ramp for truecolor inputs. Also defines `FONT_SEARCH_PATHS` and
`BOLD_FONT_SEARCH_PATHS`.

### `terminal.py` (279 lines)

PTY management and pyte terminal emulation. Forks a child shell in a
pseudo-terminal, sets `TERM=xterm-256color`, sources login profile,
and exposes non-blocking `read()`/`write()` on the master fd. Owns
the `pyte.Screen` and `Stream`, handles resize via `TIOCSWINSZ` +
`SIGWINCH`, tracks cursor movement for dirty-row detection, and
implements text selection (begin/update/end/clear) with cell-coordinate
normalization.

### `setup_screen.py` (219 lines)

VT220-style SET-UP mode overlay widget. `SetupScreen` owns its own
`pyte.Screen` + `Stream` that the existing `TerminalRenderer` draws
without modification. The overlay is composed entirely of escape
sequences (SGR reverse-video for the selected row, box-drawing
separators). `SetupField` models one configurable row with cycleable
values. Navigation keys (Up/Down/Left/Right/Return) cycle fields;
Escape applies, F3 cancels.

### `cast_recorder.py` (92 lines)

Asciinema v2 `.cast` writer. Records PTY output (and optionally input)
as newline-delimited JSON events with monotonic timestamps. Handles
incremental UTF-8 decoding to avoid splitting multi-byte sequences
across events. Emits resize (`r`) events on terminal resize.

### `cast_replay.py` (333 lines)

Offline replay and contrast inspection. `replay_cast()` feeds a `.cast`
file through `pyte` + `TerminalRenderer` and returns a `ReplayedFrame`
with the rendered PIL image. `inspect_frame()` and `inspect_pixels()`
walk every non-space cell, estimate bg via modal quantized color, find
the max-luminance-delta foreground pixel, and compute the WCAG 2.1
contrast ratio. Returns a `ContrastReport` with min/p10/median/p90/max
stats and a list of cells below threshold.

### `oklch.py` (219 lines)

Pure-math color space conversions: sRGB to/from Oklab, sRGB to/from
OKLCH. Also contains `generate_palette()`, which builds a 16-color
ANSI palette at uniform OKLCH lightness with a configurable hue bias.
Includes a `_pick_neutral_l()` solver that walks L* until a neutral
swatch clears a target WCAG contrast ratio against the theme
background.

### `supervisor.py` (201 lines)

Process supervisor for spawning `python -m vncvt` as a subprocess with
guaranteed teardown. `start_vncvt()` allocates a free port, creates a
scene-dump directory and a short-named Unix-domain control socket,
spawns the process, and waits for both the TCP listener and the socket
file to appear. `stop_vncvt()` does two-phase shutdown (SIGTERM, wait
2s, SIGKILL). `vncvt_session()` is a context-manager wrapper. Used by
the test fixtures in `conftest.py`.

### `scene_dump.py` (405 lines)

Per-scene debug artifact dumper. A "scene" is a named checkpoint that
produces four files: `scene.text` (pyte display), `scene.hex.gz`
(gzipped RGBX hex dump), `scene.fb.png` (server framebuffer PNG), and
`scene.term.json` (full pyte state + vncvt selection/cursor extras).
Also provides `serve_control_socket()`, an asyncio Unix-domain-socket
server that accepts JSON requests for `dump` and `resize` operations.

## Shared mutable state

`renderer.py` defines six module-level globals that are mutated by
`apply_theme()`:

| Global              | Type                                   | Default (amber)         |
|---------------------|----------------------------------------|-------------------------|
| `DEFAULT_BG`        | `tuple[int, int, int]`                 | `(0, 0, 0)`            |
| `DEFAULT_FG`        | `tuple[int, int, int]`                 | `(255, 190, 80)`       |
| `BOLD_FG`           | `tuple[int, int, int]`                 | `(255, 220, 120)`      |
| `CURSOR_COLOR`      | `tuple[int, int, int]`                 | `(255, 190, 80)`       |
| `_PALETTE_PHOSPHOR` | `list[tuple[int, int, int]]` (256 slots) | built from `_AMBER_PHOSPHOR` |
| `AMBER_COLORS`      | `dict[str, tuple[int, int, int]]`      | copy of `_AMBER_PHOSPHOR`   |

`AMBER_COLORS` is a back-compat alias that always points at the active
theme's phosphor palette (cleared and repopulated in place by
`apply_theme()` so existing references see the new colors).

`_PALETTE_PHOSPHOR` is rebuilt from scratch by `_build_256_phosphor()`
on every theme change.

### Callers of `apply_theme()`

1. **`__main__.py`** -- called once at startup before constructing
   `TerminalRenderer` (line 215).
2. **`server.py`** (`_apply_setup_snapshot`) -- called when the user
   exits SET-UP mode with a new theme selected (lazy import, line 402).
3. **`cast_replay.py`** (`replay_cast`) -- called before constructing
   the replay renderer (line 122).

Because these globals are read at draw time by `TerminalRenderer`
methods (`render_dirty`, `render_cursor`, `resize`, `full_render`),
the caller must call `apply_theme()` *before* any render pass for the
new colors to take effect.

## Font fallback chain

The primary font is resolved by `_find_font(FONT_SEARCH_PATHS)`, which
returns the first existing path from this ordered list:

| Priority | Path                                                              | Platform     |
|----------|-------------------------------------------------------------------|--------------|
| 1        | `/System/Library/Fonts/SFNSMono.ttf`                              | macOS (SF Mono) |
| 2        | `/System/Library/Fonts/Menlo.ttc`                                 | macOS fallback  |
| 3        | `/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf`             | Debian/Ubuntu   |
| 4        | `/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf` | Debian/Ubuntu   |
| 5        | `/usr/share/fonts/truetype/freefont/FreeMono.ttf`                 | Debian/Ubuntu   |
| 6        | `/usr/share/fonts/TTF/DejaVuSansMono.ttf`                        | Arch Linux      |
| 7        | `/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf`      | Fedora          |
| 8        | `vncvt/fonts/TerminusTTF-4.49.3.ttf`                             | Vendored fallback |

If none exist, `fc-match monospace` is tried as a last resort.

After the primary font is loaded, `TerminalRenderer.__init__` appends
two fallback fonts to the Skia fallback chain:

- **Sarasa Mono K** (`vncvt/fonts/SarasaMonoK-{Regular,Bold}.ttf`) --
  CJK / Hangul coverage. Loaded if the vendored files exist.
- **Emoji** -- first found of:
  1. `/System/Library/Fonts/Apple Color Emoji.ttc` (macOS sbix)
  2. `vncvt/fonts/NotoColorEmoji.ttf` (vendored CBDT/CBLC, OFL 1.1)

`_find_font_for_char()` walks the fallback list for each character,
returning the first Skia `Font` whose `unicharToGlyph()` returns
non-zero. Color-bitmap fonts (emoji) are flagged so `_draw_glyph()`
skips the tint `Paint.setColor` in `true-color` mode (but still tints
in `phosphor` mode to preserve the single-hue aesthetic).

## Color pipeline

A pyte color value flows through `_resolve_color()` in
`TerminalRenderer` at render time. The path depends on the
`color_mode` setting (`"phosphor"` or `"true-color"`).

### Input forms

pyte hands `_resolve_color()` one of four color representations:

1. **`"default"`** or `None` -- the terminal default.
2. **Named string** (e.g. `"red"`, `"brightcyan"`) -- one of the 16
   ANSI color names.
3. **Integer or digit string** (0--255) -- an ANSI 256-color index.
4. **6-char hex string** (e.g. `"ff8080"`) -- a 24-bit truecolor value
   from an SGR 38;2 or 48;2 sequence.

### Resolution by mode

```
          "default"             named               index 0-255          hex "rrggbb"
             |                    |                      |                     |
             v                    v                      v                     v
        phosphor:            AMBER_COLORS           phosphor:            _apply_oklab_ramp()
        BOLD_FG/DEFAULT_FG   lookup (with           _PALETTE_PHOSPHOR        |
        true-color:          bold promotion)         true-color:          phosphor: use ramp
        same                                        _STANDARD_PALETTE_256 true-color:
                                                                          bg=raw, fg=ramp
```

**Phosphor mode** (default): Every color collapses to a brightness
variant of the theme's single hue. Named colors look up `AMBER_COLORS`
(which `apply_theme()` populates with the active theme's 16-slot
phosphor palette). Integer indices look up `_PALETTE_PHOSPHOR` (a
256-slot palette built by `_build_256_phosphor()`, where slots 16-231
map xterm cube luminance onto the theme's black-to-brightwhite ramp).
Hex truecolor inputs are fed through `_apply_oklab_ramp()`, which
converts the input to Oklab L*, applies a gamma-0.4 lift with an
L_eff floor of 0.5, and interpolates along the theme's bg-to-fg
axis.

**True-color mode**: Named colors still use `AMBER_COLORS` (theme
phosphor palette), integer indices use `_STANDARD_PALETTE_256`
(standard xterm 256-color table, not theme-tinted), and hex inputs
pass through raw for backgrounds but go through the OKLab ramp for
foregrounds (lifting dim greys to clear WCAG AA against the theme bg).

### OKLab ramp (`_apply_oklab_ramp`)

The OKLab ramp is the shared readability lift for truecolor fg values.
It:

1. Converts the input RGB to Oklab L*.
2. Picks a reference frame: if the cell's actual bg is known, chooses
   a light-pushes-dark or dark-pushes-light orientation; otherwise
   falls back to `DEFAULT_BG` / `DEFAULT_FG`.
3. Flips L_eff for dark-on-light themes so the ramp always goes
   "user intent low -> bg pole, user intent high -> fg pole".
4. Floors L_eff at 0.5 so near-bg inputs get enough lift to be
   readable (~9:1 contrast minimum).
5. Applies a 0.4-exponent gamma lift, tuned against Claude Code's
   mid-grey hint colors (#808080, #949494, #d78787) to clear WCAG AA
   (4.5:1) on every built-in theme.
6. Linearly interpolates between the reference bg and fg poles at the
   lifted parameter.

## Test suite structure

Tests live under `tests/` in three tiers, plus shared helpers.

### Unit tests (`tests/unit/`, ~39 collected)

No server process, no VNC client. Directly import and exercise the
Python modules:

- `test_setup_screen.py` (14 tests) -- `SetupField` cycling,
  `SetupScreen` layout, key navigation, snapshot extraction.
- `test_cast_replay.py` (3 functions, 7+ parametrized) -- cast
  loading, frame replay, per-theme row-0 readability regression.
- `test_color_mode.py` (3 functions, 14+ parametrized) --
  phosphor/true-color contrast floor across all 7 themes.

### Integration tests (`tests/integration/`, ~40 collected)

Spawn a real `vncvt` server via `supervisor.start_vncvt()` and connect
an `asyncvnc` VNC client. Each test gets a fresh server instance.

- `test_handshake.py` -- RFB 3.8 connect + initial FBU.
- `test_vnc_auth.py` -- password auth, wrong-password rejection, RFB
  3.3 downgrade.
- `test_resize.py` -- `SetDesktopSize` round-trip + cell-grid snap.
- `test_padding.py` -- corner-pixel color matches `DEFAULT_BG`.
- `test_paste.py` -- `ClientCutText` echoed back via PTY.
- `test_selection.py` -- drag-to-select + `ServerCutText`.
- `test_cursor_refresh.py` -- cursor row dirtied on move.
- `test_unicode_fallback.py` -- CJK + emoji glyph rendering.
- `test_setup_mode.py` -- F3 toggle + Escape apply.
- `test_setup_cycling.py` -- parametrized field cycling.
- `test_apple_pixelfmt.py` -- BGRA pixel format negotiation.
- `test_cli.py` -- `--info` JSON, `--mode`, `--record`, config file
  loading.

### End-to-end tests (`tests/e2e/`, marker-gated)

Gated by custom pytest markers; skipped unless the required tooling
is available:

- `test_loopback.py` (`@pytest.mark.loopback`) -- Linux: vncdotool
  types a command and verifies the scene dump. macOS: Screen
  Sharing.app connect + screencapture crop.
- `test_claude_code.py` (`@pytest.mark.claude_code`) -- requires
  `claude` CLI on PATH. Spawns Claude Code inside vncvt and verifies
  scene snapshots.
- `test_calibration.py` (`@pytest.mark.loopback`) -- scene-match
  threshold calibration helper.

### Shared helpers

- `tests/conftest.py` -- pytest fixtures wrapping `supervisor.py`.
- `tests/helpers.py` -- `asyncvnc`-based VNC client utilities.
- `tests/scenes.py` -- scene-dump trigger + client-side PNG capture.
- `tests/baselines.py` -- dHash baseline loading for scene matching.
- `tests/check_scene_match.py` -- standalone scene-match verifier
  (used in CI).
- `tests/crop_and_hash_macos.py` -- macOS screencapture crop + hash.
