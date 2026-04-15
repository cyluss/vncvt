# vncvt

VNC terminal server with a VT220 amber aesthetic. Serves an interactive shell over the RFB protocol, rendered with Skia + LCD AA on a configurable OKLCH color palette. Compatible with any VNC client including macOS Screen Sharing.app.

- **Protocol**: RFB 3.3 / 3.7 / 3.8, VNC Authentication, raw + zlib encodings
- **Terminal**: pyte VT100/VT102 emulator in a real PTY
- **Rendering**: Skia TrueType hinting, LCD filtering, 16/256/24-bit color tiers
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

## Theme showcase

Five built-in themes. OKLCH-generated 16-color ANSI palettes at uniform perceived lightness. All themes clear WCAG AAA on peak contrast.

Switch themes three ways:

1. `--theme NAME` at launch
2. `theme = "…"` in the config file
3. **F3** → arrow to `Theme` → Return → Escape

### `amber` — warm amber on black *(default)*

![amber](docs/screenshots/claude-code-amber.png)

Measured: modal **9.91:1** / peak 12.73:1.

### `green` — phosphor green on black

![green](docs/screenshots/claude-code-green.png)

Measured: modal **12.10:1** / peak 16.37:1.

### `light` — black on white

![light](docs/screenshots/claude-code-light.png)

Measured: modal **15.91:1** / peak 21.00:1.

### `dark` — white on black

![dark](docs/screenshots/claude-code-dark.png)

Measured: modal **15.46:1** / peak 20.12:1.

### `powershell` — off-white on navy `#012456`

![powershell](docs/screenshots/claude-code-powershell.png)

Measured: modal **11.00:1** / peak 12.97:1.

*Modal* = WCAG ratio between the two most common colors in the rendered screenshot. *Peak* = max fg/bg contrast achievable by the palette. Captured at 80×24, SF Mono 13pt, `line-height 1.1`, `contrast max`.

## Color modes

Four fidelity tiers (Windows 95 color-depth taxonomy). Only `true-color` renders TUI native palettes; the other three are theme-tinted.

| Mode          | ANSI input                       | Truecolor input                   | Aesthetic            |
|---------------|----------------------------------|-----------------------------------|----------------------|
| `monochrome`  | Theme bg→fg OKLab ramp           | Same ramp                         | VT220 phosphor       |
| `16-color` *(default)* | Theme ANSI 0–15, bg-aware snap | Snapped to nearest of 16  | EGA/CGA chunky       |
| `256-color`   | Theme OKLCH 256 palette          | Theme ramp                        | VGA theme-tinted     |
| `true-color`  | Standard xterm 256 palette       | Raw passthrough (fg keeps lift)   | Native — Claude Code in its own colors |

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

- `--theme NAME` — `amber` (default), `green`, `light`, `dark`, `powershell`
- `--color-mode MODE` — `monochrome`, `16-color` (default), `256-color`, `true-color`
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
color-mode = "16-color"
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
for theme in amber green light dark powershell; do
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
