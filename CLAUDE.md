# vncvt — Claude Code project context

vncvt is a VNC terminal server that renders a PTY with a VT220 amber
aesthetic. See `README.md` (if present) for user-facing docs.

## Start here

If you're a fresh Claude Code session picking up active work on the
`claude/vnc-terminal-server-YAmN5` branch, read the most recent
handoff document first:

- [`docs/session-handoff-2026-04-13.md`](docs/session-handoff-2026-04-13.md)

It summarizes the current open problem (Apple Screen Sharing.app
hanging at "Connecting..." on macos-latest), the commits in flight,
what's been ruled out, what to do first, and the calibration task
that's waiting. It's ~300 lines. Read it top-to-bottom before
touching anything.

For earlier sessions' handoffs see other files matching
`docs/session-handoff-*.md` if any exist.

## Branch discipline

All active development happens on `claude/vnc-terminal-server-YAmN5`.
Never push to a different branch without explicit user permission.

## Test commands

```
uv run pytest tests/ -v                    # full local suite (13 tests)
uv run --with scikit-image --with pillow \
    python tests/check_scene_match.py <scene_dir>   # scene match check
```

## Layout

- `vncvt/` — the server package (RFB protocol, renderer, terminal,
  scene dumper)
- `tests/` — pytest suite, fixtures, and the scene-verification
  helpers (`check_scene_match.py`, `crop_and_hash_macos.py`)
- `.github/workflows/loopback-test.yml` — CI with 3 jobs:
  `linux-pytest`, `linux-vncdo-cross-check`, `macos-screen-sharing`
- `docs/` — session handoff documents
