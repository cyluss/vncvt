# vncvt

VNC terminal server with a **warm amber VT220 aesthetic** by default. Serves an interactive shell over the RFB protocol, rendered with Skia + LCD AA on a configurable OKLCH color palette. Compatible with any VNC client including macOS Screen Sharing.app.

![amber](docs/screenshots/claude-code-amber.png)

*Default theme: `amber` — warm amber phosphor on black. Six more themes (`green`, `dark`, `light`, `c64`, `dos`, `atari`) in [docs/SHOWCASE.md](docs/SHOWCASE.md).*

- **Protocol**: RFB 3.3 / 3.7 / 3.8, VNC Authentication, raw + zlib encodings
- **Terminal**: pyte VT100/VT102 emulator in a real PTY
- **Rendering**: Skia TrueType hinting, LCD filtering, phosphor single-hue or true-color palette
- **Input**: full keyboard, Ctrl/Alt/Meta, paste, Shift+Tab, function keys
- **Live config**: F3 SET-UP mode for theme, columns, font size, FPS without restart
- **Recording**: asciinema v2 `.cast` format, replayable through vncvt's renderer

## Install

Requirements:

- Linux or macOS
- Python 3.11+
- [uv](https://docs.astral.sh/uv/)

Run it:

```bash
uv run vncvt                      # listen on 127.0.0.1:5900, amber theme
open "vnc://:PASSWORD@127.0.0.1"  # macOS Screen Sharing.app
```

## Themes

Seven built-in themes. Switch any of three ways:

1. `--theme NAME` at launch
2. `theme = "…"` in the config file
3. **F3** → arrow to `Theme` → Return → Escape

See [**docs/SHOWCASE.md**](docs/SHOWCASE.md) for per-theme screenshots, measured contrast ratios, and palette-generation notes.

## Color modes

Two modes. Only `true-color` renders TUI native palettes; `phosphor` is theme-tinted.

| Mode                    | ANSI input                   | Truecolor input                 | Aesthetic                                   |
|-------------------------|------------------------------|---------------------------------|---------------------------------------------|
| `phosphor` *(default)*  | Theme phosphor palette       | OKLab ramp → theme primary hue  | Single-hue CRT — every intensity is a shade of the theme fg |
| `true-color`            | Standard xterm 256 palette   | Raw passthrough (fg lifted)     | Native — Claude Code in its own colors      |

Set via `--color-mode MODE`, the `color_mode =` config key, or F3 → `Color mode`.

## CLI options

Server:

- `--host HOST` — listen address (default: `127.0.0.1`)
- `--port PORT` — RFB port (default: `5900`)
- `--password PW` — enable VNC Auth (required for macOS Screen Sharing.app)
- `--shell SHELL` — shell to run (default: `$SHELL`)
- `--fps N` — framebuffer rate cap, 1–120 (default: `15`)

Terminal:

- `--cols COLS` — columns (default: 80)
- `--rows ROWS` — rows (default: 24)
- `--mode MODE` — VT220 preset: `80x24` or `132x24` (mutually exclusive with `--cols`/`--rows`)

Rendering:

- `--theme NAME` — `amber` (default), `green`, `dark`, `light`, `c64`, `dos`, `atari`
- `--color-mode MODE` — `phosphor` (default, single-hue CRT), `true-color` (native xterm + raw RGB)
- `--contrast LEVEL` — `normal`, `high`, `max` (default). Drop to `normal` on hi-DPI
- `--font PATH` — TTF font (default: SF Mono → DejaVu → vendored Terminus)
- `--font-size SIZE` — points (default: 13)
- `--line-height MULT` — 0.8–2.0 (default: 1.1)

Config & diagnostics:

- `--config PATH` — TOML file (default: `~/.config/vncvt/config.toml`)
- `--no-config` — skip the file
- `--record PATH` — write an asciinema v2 `.cast` recording of the PTY
- `--info` — print a JSON diagnostic (version, defaults, font search path) and exit

## Config file

Persistent defaults live at `~/.config/vncvt/config.toml` (or `$XDG_CONFIG_HOME/vncvt/config.toml`). Keys mirror the CLI flags (kebab-case allowed):

```toml
theme = "amber"
color-mode = "phosphor"
contrast = "max"
font-size = 13
line-height = 1.1
```

- CLI flags override config values
- `--no-config` skips the file entirely
- Unknown keys cause a hard error

## VT220 SET-UP mode

Press **F3** from any connected client to open an overlay modeled on the VT220's hardware configuration screen. F3 is intercepted — it never reaches the shell.

| Key           | Action                           |
|---------------|----------------------------------|
| `F3`          | Toggle SET-UP (cancel if active) |
| `Up` / `Down` | Navigate fields                  |
| `Return`      | Cycle selected field forward     |
| `Left`        | Cycle backward                   |
| `Escape`      | Apply and exit                   |

Editable fields:

- Columns, Rows, Font size, FPS
- Theme, Line height, Contrast, **Color mode**

## Recording & replay

vncvt records every PTY byte it receives to an asciinema v2 `.cast` file:

```bash
uv run vncvt --record /tmp/session.cast
```

The file is standard asciinema, so you can:

- `asciinema play session.cast`
- Convert to GIF: [`agg`](https://github.com/asciinema/agg)
- Convert to animated SVG: [`svg-term-cli`](https://github.com/marionebl/svg-term-cli)
- Replay through vncvt's own renderer (next section)

### Offline replay through vncvt

`scripts/replay_cast.py` feeds a `.cast` back through `TerminalRenderer` and dumps a PNG plus per-cell WCAG contrast report. Useful for theme/contrast regression testing without re-running the live session:

```bash
uv run python scripts/replay_cast.py /tmp/session.cast \
    --theme amber --color-mode true-color --at end --out /tmp/replay.png
```

- `--at SEC` replays up to a specific timestamp (default: end)
- `--theme` / `--color-mode` / `--contrast` select the render pipeline to test
- `--json PATH` also dumps a structured contrast report

Replay the same `.cast` in every theme:

```bash
for theme in amber green dark light c64 dos atari; do
  uv run python scripts/replay_cast.py /tmp/session.cast \
      --theme $theme --out /tmp/replay-$theme.png
done
```

## Testing

```bash
uv run pytest tests/ -q              # full local suite
uv run pytest tests/ -m claude_code   # optional: requires `claude` on PATH
```

The suite covers:

- **Unit** — renderer color resolution, color-mode tiers, cast loading
- **Integration** — CLI `--info`, SET-UP cycling, session persistence
- **End-to-end** — RFB handshake, resize, Claude Code scene regression

## Architecture

```
VNC client ⇄ RFB (vncvt/server.py)
                 │
                 ├── vncvt/renderer.py — Skia + OKLCH palettes + color modes
                 │
                 ├── vncvt/terminal.py — PTY + pyte Screen/Stream
                 │
                 ├── vncvt/setup_screen.py — F3 overlay widget
                 │
                 ├── vncvt/cast_recorder.py — asciinema v2 writer
                 │
                 └── vncvt/cast_replay.py — replay + contrast inspector
```

## References

### Motivation

- Joel Buckley, [*"OS X with a VT220, Part 1"*](https://blog.joelbuckley.com.au/2021/07/os-x-vt220-part-1) — connecting a DEC VT220 to a 2017 MacBook Pro via USB-C → RS-232 → null modem, 9600 baud `getty` on macOS. The amber VT220 died from a flyback transformer failure partway through; the write-up ends on a white-phosphor VT510. This is the gap vncvt fills for anyone whose market doesn't carry working amber CRTs.
- Drew DeVault, [*"Integrating a VT220 into my life"*](https://drewdevault.com/2016/03/22/Integrating-a-VT220-into-my-life.html) — a VT220 wired into a sway compositor as "almost a fourth monitor" via a fake output plus tmux auto-attach, used daily for mutt. The proof that the device earns its desk space when it's a real workflow target, not just a curiosity.
- Hackaday (2024), [*"A Look at the DEC VT220, a Proper Serial Terminal"*](https://hackaday.com/2024/07/17/a-look-at-the-dec-vt220-a-proper-serial-terminal/) — a 2024 showcase of working VT220 hardware and a reminder that we call our terminals "terminal emulators" for a reason. The comment thread surfaces the amber-screen nostalgia that drove this project.
- Laughing Squid, [*"How To Connect a Vintage VT220 Terminal to a Mac Pro"*](https://laughingsquid.com/how-to-connect-a-vintage-vt220-terminal-to-a-mac-pro/) — the Justin Oullette photos that made the setup look irresistible in the first place.

### Standards & formats

- [RFB Protocol 3.8](https://datatracker.ietf.org/doc/html/rfc6143) — what vncvt speaks to VNC clients
- [DEC VT220 Programmer Reference](https://vt100.net/docs/vt220-rm/) (HTML) and [Bitsavers VT220 archive](https://bitsavers.trailing-edge.com/pdf/dec/terminal/vt220/) (original PDFs) — the SET-UP mode field layout and key handling
- [Asciinema v2 cast format](https://docs.asciinema.org/manual/asciicast/v2/) — `--record` output
- [WCAG 2.1 contrast ratio](https://www.w3.org/TR/WCAG21/#contrast-minimum) — the floor the cast replay inspector measures against

### Color science

- [OKLab / OKLCH](https://bottosson.github.io/posts/oklab/) by Björn Ottosson — the perceptual color space behind the uniform-lightness ANSI palettes and the fg readability lift
- [Xterm 256-color palette](https://jonasjacek.github.io/colors/) — the `true-color` mode lookup table

### Libraries

- [pyte](https://github.com/selectel/pyte) — VT100/VT102 screen emulator
- [skia-python](https://github.com/kyamagu/skia-python) — the Skia binding used for glyph rasterization
- [asyncvnc](https://github.com/barneygale/asyncvnc) — the test-side RFB client
- [agg](https://github.com/asciinema/agg) / [svg-term-cli](https://github.com/marionebl/svg-term-cli) — convert recorded `.cast` files to GIF / animated SVG

