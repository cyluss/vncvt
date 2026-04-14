# vncvt CLI flags + VT220 SET-UP mode overlay

## Context

The user is reading the VT220 Technical Manual and wants vncvt to mirror two
specific things from the real hardware:

1. **Configurable runtime parameters** — frame rate and screen dimensions as
   CLI flags. Today the update loop sleeps a hardcoded `1/30` second and
   `--cols`/`--rows` exist but with no preset for the VT220's two canonical
   widths (80 and 132 columns, selected by the DECCOLM escape sequence
   `\e[?3h` / `\e[?3l` on the real hardware).

2. **SET-UP mode overlay** mimicking the VT220's in-terminal configuration
   UI — entered on real hardware via the SET-UP key, emulated here as F3
   (X11 keysym `0xFFC0`, the same sequence vncvt currently sends to the
   PTY as `\x1bOR`). Rendered as a separate `pyte.Screen` the existing
   `TerminalRenderer` draws through the same code path as the live PTY
   screen, with field navigation via arrow keys and apply-on-exit that
   reuses `RFBServer.handle_resize` for cols/rows.

User clarified "service mode" means **SET-UP mode** (in-terminal config),
not the VT220's hardware self-test mode — the latter has no software
analogue on an emulator.

Work is split into two phases:

- **Phase 1**: three mechanical CLI flags (`--fps`, `--mode`, `--info`).
  One commit, no architectural decisions.
- **Phase 2**: the SET-UP mode overlay. Real feature, 6–8 commits,
  introduces an `_active_screen` indirection on `RFBServer` and a new
  `vncvt/setup_screen.py` widget module.

Everything ships on `claude/vnc-terminal-server-YAmN5`.

## Approach

Phase 1 is three argparse additions and one small plumbing thread: `fps`
becomes an attribute on `RFBServer` that `_update_loop` reads every tick,
`--mode` is resolved in `main()` before constructing anything, and
`--info` short-circuits before the event loop starts.

Phase 2 keeps a single `TerminalRenderer` and a single active-screen
pointer on `RFBServer` (`_active_screen`), swapping between
`terminal.screen` and a second `pyte.Screen` owned by a new `SetupScreen`
widget. The update loop and all render calls dereference
`self._active_screen` inside `_resize_lock`, so swaps are atomic with
respect to the render+send pass. `SetupScreen` writes its current
rendering into its pyte.Screen via a `feed(...)` stream using real VT
escape sequences (SGR reverse-video for the selected row), which means
the existing renderer draws it for free with zero renderer changes. Key
routing gains one `if self.server._in_setup:` branch in
`RFBClient._message_loop`. Apply-on-exit reuses `handle_resize` for
cols/rows, mutates `server.fps` in place for FPS, and for font-size
constructs a replacement `TerminalRenderer` under `_resize_lock`.

## Phase 1 — CLI flags (one commit, ~6 tasks)

**P1.1** Add `--fps N` to argparse in `vncvt/__main__.py` (after
`--font-size`). Default 30, valid range 1–120. Validate in `main()` with
`parser.error`. Thread as `fps` param to `RFBServer.__init__`
(`server.py:205-225`), store as `self.fps`. Replace `_update_loop`'s
`await asyncio.sleep(1 / 30)` at `server.py:257` with
`await asyncio.sleep(1 / max(1, self.fps))`. Reading `self.fps` every
tick is the whole mechanism — Phase 2 mutates the same attribute live.

**P1.2** Add `--mode 80x24|132x24` preset. `choices=("80x24", "132x24")`,
mutually exclusive with `--cols`/`--rows`. Switch `--cols`/`--rows`
defaults to `None`, resolve in `main()`: if `--mode` set, split on `x`,
assign to `args.cols` / `args.rows`, error on collision; else apply
defaults 80 / 24. File: `vncvt/__main__.py`.

**P1.3** Add `--info` one-shot diagnostic. `action="store_true"`. Handled
right after mode pre-processing, before constructing the renderer/server.
Print JSON with:
- `version`: `importlib.metadata.version("vncvt")`
- `defaults`: host/port/cols/rows/font_size/fps/shell from args
- `font_search_path`: `list(FONT_SEARCH_PATHS)` from `renderer.py`
- `font_found`: `args.font or _find_font(FONT_SEARCH_PATHS)`
- `listen`: `f"{args.host}:{args.port}"`

Then `return` from `main()` before starting the event loop. File:
`vncvt/__main__.py`.

**P1.4** `tests/test_cli.py` — spawn `vncvt_server_factory("--fps", "60")`
and assert the server starts without crashing. Add error-path test via
`subprocess.run([..., "--fps", "200"])` asserting non-zero exit and
`"--fps must be between"` in stderr. Mirror for `--mode 132x24` (assert
framebuffer width via asyncvnc `client.video.width`) and the
`--mode 132x24 --cols 100` mutual-exclusion error path.

**P1.5** `tests/test_cli.py::test_info_json` — `subprocess.run([sys.executable,
"-m", "vncvt", "--info"], capture_output=True)`, `json.loads` stdout,
assert version and font_found are present and non-empty. Assert nothing
listening on 5900 after exit (process should have returned).

**P1.6** Full `pytest tests/ -q` green at end of commit. No existing
tests touch the default-path codepath so they should pass unchanged.

## Phase 2 — SET-UP mode overlay (6–8 commits)

**P2.1 — refactor: `_active_screen` indirection (no behavior change)**.
In `RFBServer.__init__` (`server.py:205-225`) add:

```python
self._active_screen = terminal.screen
self._in_setup = False
self._setup: "SetupScreen | None" = None
```

Replace every `self.terminal.screen` in `_update_loop` (254-287),
`handle_resize` (289-314), and `_message_loop` FramebufferUpdateRequest
branch (~561) with `self._active_screen`. Add a `_current_dirty_rows()`
helper that branches on `_in_setup` to read from the correct screen's
`.dirty` set. Full `pytest tests/ -q` green, zero behavior change.
Refactor commit; load-bearing for P2.5 and later.

**P2.2 — renderer: stash font_size + font_path**. In
`TerminalRenderer.__init__` around `renderer.py:168`, add
`self.font_size = font_size` and (after the `_find_font` resolution)
`self._font_path = font_path`. Enables P2.5's apply-on-exit font rebuild
without guessing `font.path`. One-line commit. File: `vncvt/renderer.py`.

**P2.3 — new module `vncvt/setup_screen.py`**. Classes:

- `SetupField(label: str, values: list, index: int = 0, readonly: bool = False)`
  with `current()` and `cycle()`.
- `SetupScreen(cols, rows, initial: dict, server_info: dict)` owns:
  - `self.screen = pyte.Screen(cols, rows)`
  - `self.stream = pyte.Stream(self.screen)`
  - `self.fields: list[SetupField]` — Columns (80/132), Rows (24/36/48),
    Font size (8-32), FPS (curated list), Cursor style, Cursor blink,
    Background, Server (readonly), Clients (readonly).
  - `self.selected: int = 0`
  - `on_key(keysym: int) -> bool`: Up (0xFF52), Down (0xFF54), Return
    (0xFF0D) → cycle. Returns True when consumed.
  - `redraw()`: feeds SGR escape sequences into `self.stream`, marking
    the selected row with `\x1b[7m` reverse video. Renderer draws this
    via the existing SGR path with zero new code.
  - `snapshot() -> dict`: returns `{label: current_value}` for all
    fields, used by `_apply_setup_snapshot` in P2.5.

Pure Python, no server dependency. New file.

**P2.4 — unit test `tests/test_setup_screen.py`**. Direct instantiation,
feed key events, assert `snapshot()` transitions, assert `screen.display`
strings contain expected labels. Cover: navigation wraparound,
readonly-field cycle is a no-op, selected row contains reverse-video
SGR output. No server, no VNC client.

**P2.5 — `enter_setup` / `exit_setup` / `_apply_setup_snapshot` on
RFBServer**. New async methods on `RFBServer`:

- `enter_setup()`: acquire `_resize_lock`, instantiate `SetupScreen` with
  current config, swap `_active_screen`, mark all rows dirty, broadcast
  a full update.
- `exit_setup(apply: bool)`: acquire `_resize_lock`, snapshot the setup
  screen if applying, swap `_active_screen` back to `terminal.screen`,
  mark all rows dirty, broadcast. After releasing the lock, if applying,
  call `_apply_setup_snapshot(snap)`.
- `_apply_setup_snapshot(snap)`: mutate `self.fps`; if font_size changed,
  rebuild `TerminalRenderer` under the lock (reusing `_font_path`); if
  cols/rows changed, call existing `handle_resize`. Order: fps → font →
  cols/rows.

File: `vncvt/server.py`.

**P2.6 — key routing in `_message_loop` KeyEvent branch** (server.py:
569-581). Restructure:

- F3 (`0xFFC0`) toggles SET-UP mode regardless of state; captured so
  clients never get `\x1bOR` (documented F3 binding loss).
- If `_in_setup`: Escape (0xFF1B) → `exit_setup(apply=True)`; other
  navigation keys → `self.server._setup.on_key(keysym)`; unhandled keys
  dropped (don't reach PTY).
- Otherwise: existing PTY path via `keysym_to_bytes`.

File: `vncvt/server.py`.

**P2.7 — e2e test `tests/test_setup_mode.py`**. Uses `vnc` fixture.
Baseline screenshot, press F3 (via whatever keysym API asyncvnc exposes;
check `tests/test_paste.py` for pattern), screenshot, assert overlay
header row has high amber-pixel density. Navigate down, press Return,
press Escape to apply+exit, screenshot, assert the change took effect
(e.g. cols/rows changed).

**P2.8 — docs**. Short SET-UP mode section in `README.md` or a new
`docs/` entry: F3 toggles SET-UP, arrows navigate, Return cycles,
Escape applies + exits, F3 cancels without applying. Document that
F3 no longer reaches the shell as `\x1bOR`.

## Critical files

- `vncvt/__main__.py:17-88` — Phase 1 argparse + main() validation/info
- `vncvt/server.py:205-225` — `RFBServer.__init__` (fps, `_active_screen`,
  `_in_setup`, `_setup`)
- `vncvt/server.py:254-287` — `_update_loop` (fps sleep + `_active_screen`)
- `vncvt/server.py:289-314` — `handle_resize` (`_active_screen`)
- `vncvt/server.py:569-581` — `_message_loop` KeyEvent branch (F3 routing)
- `vncvt/server.py` — new `enter_setup` / `exit_setup` /
  `_apply_setup_snapshot` methods
- `vncvt/renderer.py:150-208` — stash `self.font_size`, `self._font_path`
- `vncvt/setup_screen.py` — NEW, `SetupField` + `SetupScreen`
- `tests/test_cli.py` — NEW, Phase 1 argparse + --info tests
- `tests/test_setup_screen.py` — NEW, widget unit tests
- `tests/test_setup_mode.py` — NEW, e2e key-routing test

## Verification

- **P1.1**: `python -m vncvt --fps 5 --port 59000` visible-slow smoke;
  `pytest tests/ -q` passes.
- **P1.2**: `python -m vncvt --mode 132x24 --port 59000` binds;
  `python -m vncvt --mode 132x24 --cols 100` exits non-zero with
  mutual-exclusion error.
- **P1.3**: `python -m vncvt --info | python -m json.tool` yields valid
  JSON, exit 0; `ss -ltn` confirms nothing listening.
- **P1.4/P1.5**: `pytest tests/test_cli.py -q`.
- **P2.1**: full `pytest tests/ -q` green (refactor-only).
- **P2.2**: full `pytest tests/ -q` green (new attribute, unused).
- **P2.3/P2.4**: `pytest tests/test_setup_screen.py -q` — covers
  SetupScreen in isolation, no server.
- **P2.5/P2.6**: manual VNC client — press F3, overlay appears; Escape
  applies; PTY resumes.
- **P2.7**: `pytest tests/test_setup_mode.py -q`.
- **Full regression**: `pytest tests/ -q` green at every commit boundary.

## Risks

1. **Screen-shape drift during external resize.** If a client sends
   SetDesktopSize while `_in_setup`, the setup screen's shape mismatches
   the terminal's. Mitigation: reject external resize when `_in_setup`,
   OR rebuild the setup screen at the new cols/rows. Recommend reject +
   document.

2. **`_resize_lock` held across client I/O.** `enter_setup`/`exit_setup`
   await network writes inside the lock. Matches existing
   `handle_resize` pattern but slow clients can stall the render loop.
   Acceptable for v1; matches existing risk envelope.

3. **F3 binding collision.** F3 currently sends `\x1bOR` to the shell.
   After P2.6, F3 is captured by SET-UP mode and never reaches the PTY.
   Low-risk user-visible change; document in commit body and
   SET-UP docs section.

4. **asyncvnc keysym API.** The e2e test assumes
   `.keyboard.press(...)` / `.down(...)` / `.up(...)` with raw keysyms.
   Verify against `tests/test_paste.py` pattern before writing the
   test; fall back to sending a raw RFB KeyEvent message if asyncvnc
   doesn't expose keysyms directly.

5. **Pillow `font.path` unreliable across versions.** We sidestep this
   entirely by stashing `self._font_path` in `TerminalRenderer.__init__`
   (P2.2) rather than reading it off the `ImageFont` instance.

6. **`_update_loop` dirty-row branching.** Without the
   `_current_dirty_rows()` helper introduced in P2.1, the update loop
   would read `terminal.screen.dirty` while rendering from
   `_setup.screen`, producing blank-frame artifacts. Helper branches on
   `_in_setup` to read the correct screen's dirty set.

7. **Phase 1 --fps test is a smoke test, not a cadence assertion.**
   Frame-rate measurement over the wire is racy. P1.4 only asserts the
   server accepts `--fps 60` and runs. Behavioral coverage for the
   runtime fps mutation lands in P2.5+ via the setup-mode e2e test.
