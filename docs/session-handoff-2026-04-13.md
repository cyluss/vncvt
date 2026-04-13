# Session handoff — 2026-04-13

**Read this first.** You are a fresh Claude Code session picking up the
work of the one that landed commits `414a176` through `cd49edd` on the
`claude/vnc-terminal-server-YAmN5` branch. This doc tells you:

1. Where the repo is
2. What the open problem is
3. What's been ruled out and why
4. What to do first on your local Mac
5. The calibration task that's waiting for you

## TL;DR for the next session

The vncvt VNC terminal server works correctly on Linux (vncdo loopback
smoke-tests pass, 13/13 pytest green). The open problem is that
**Apple's Screen Sharing.app hangs at its "Connecting to 127.0.0.1..."
progress dialog when connecting to vncvt on the macos-latest GitHub
Actions runner**. The previous session fixed a real RFB handshake bug
that was very likely (but not yet empirically confirmed) the cause, and
added full protocol tracing so the next run is diagnosable. The current
CI run is believed to still fail the new server↔client scene check on
the macOS job until you verify the handshake fix actually resolves the
Screen Sharing hang — which requires a real Mac with Screen Sharing.app,
hence this handoff.

## Branch state

- **Branch**: `claude/vnc-terminal-server-YAmN5`
- **Upstream**: `origin/claude/vnc-terminal-server-YAmN5` (pushed)
- **Local pytest**: 13/13 green on Linux
- **Uncommitted changes**: none expected (the stop hook blocks
  committing with a dirty tree)

### Today's commits, oldest to newest

| SHA       | What and why |
|-----------|--------------|
| `414a176` | Migrate regression tests from custom asyncio runners to pytest + asyncvnc + imagehash. Everything the old suite did is now a `tests/test_*.py`. |
| `b150c08` | CI fix: install vncdotool via `uv run --with` (not apt), add Screen Sharing window-crop helper and dHash baseline for drift checks. |
| `e68b746` | Add per-scene debug artifact capture. A "scene" is a named checkpoint bundle of four server files (`scene.fb.png`, `scene.hex.gz`, `scene.term.json`, `scene.text`) plus a client-side `scene.client.png`. Exposed via a Unix-domain socket control channel so shell CI jobs and pytest fixtures all trigger dumps the same way. |
| `1cf8d8f` | Switch the scene control socket from line-oriented ASCII (`DUMP <name>\n`) to JSON request/response over the same socket. Uniform shape, versioned, richer errors. |
| `8493ced` | **Fix the RFB security-types handshake bug + add diagnostics.** See "What's been ruled out" below. This is the commit that almost certainly fixes the Screen Sharing hang but you need to verify it on a real Mac. |
| `ca46477` | Add v1 heuristic scene-match check (dHash + std-dev band). Placeholder for the research-backed rewrite. |
| `cd49edd` | Replace the v1 heuristic with **SSIM (scikit-image) + HSV histogram Bhattacharyya + blank guard**. Research-backed, literature-default thresholds. |

## The open problem

On the macos-latest runner, the previous session's CI artifact showed:

- **server-side `scene.fb.png`**: vncvt rendered `bash-3.2$` prompt with
  the macOS "default interactive shell is now zsh" login banner (amber
  on black, correct)
- **client-side `scene.client.png`**: macOS Dock + Screen Sharing.app's
  "Connecting to 127.0.0.1..." progress dialog

Screen Sharing never completed the RFB handshake. The window-ID lookup
fell back to a full-desktop capture (`screencapture -x -o`, see
`.github/workflows/loopback-test.yml` line ~146), producing a PNG that
showed the macOS desktop instead of a Screen Sharing remote window.

The previous session could not directly observe *where* in the handshake
Screen Sharing gave up, because:

1. No `gh` CLI was available in the environment, so no access to Actions
   run logs or artifacts from inside the Claude Code tool surface
2. No local Mac, so no way to reproduce Screen Sharing.app's behavior

Both of those constraints should be lifted in your environment — you're
on a Mac with `gh` presumably available via Homebrew.

## What's been ruled out (and why)

### Handshake security-types bug — *likely cause, fixed in 8493ced*

**The bug**: when `--password` was set, `vncvt/server.py:_handshake`
wrote `bytes([2, 2, 1])` meaning "2 types offered: VNC auth (2) AND
None (1)". Two problems from one line:

1. **Security hole**: any client could pick type 1 (None) and skip auth
   entirely. `test_vnc_auth.py` happened to pass because asyncvnc picks
   VNC auth when both are offered with a password — but nothing
   enforced exclusivity.
2. **Screen Sharing confusion**: Apple's Screen Sharing.app is
   reported to stall at "Connecting..." when a server advertises
   both 2 and 1 simultaneously. It's unclear from published
   documentation exactly why, but the symptom matches what we saw.

**The fix** (`8493ced`): write `bytes([1, 2])` when password is set —
exactly one type offered: VNC auth. Added
`test_password_offers_only_vnc_auth` as a regression guard.

**Verification status**: Unverified on real Screen Sharing.app. Verified
green against asyncvnc locally. This is your first empirical task.

### Silent desync on unknown RFB messages — *also fixed in 8493ced*

Previously the message loop logged unknown message types and
continued — but payload bytes stayed in the reader and the next
`readexactly(1)` would interpret payload as a message header. That
would hang the connection forever from the client's perspective with
no visible error. Now unknown types raise `ConnectionError` immediately.

If Screen Sharing.app sends an Apple-specific RFB extension we don't
recognize, this will now turn a silent hang into a fast, diagnosable
failure in the next CI run. You'll see it in `/tmp/vncvt-server.log`.

### RFB protocol tracing — *added in 8493ced*

New `_trace()` helper in `vncvt/server.py`, gated on
`VNCVT_LOG_RFB=1`. Both CI jobs now export that env var and pass
`--log-traffic`, and redirect the server's stdout+stderr to
`/tmp/vncvt-server.log`, which is uploaded as part of the
`scene-dumps-*` artifact. Full smoke-test output format verified on
Linux with asyncvnc.

This means the next macOS CI run will produce a complete RFB byte log
showing exactly where Screen Sharing gives up. If you trigger a CI run
before testing locally, grab that artifact and read the log first.

## What you should do first (empirical plan)

### Step 1: Reproduce the Screen Sharing hang locally

```
git fetch origin claude/vnc-terminal-server-YAmN5
git checkout claude/vnc-terminal-server-YAmN5
uv sync

# Terminal 1 — start vncvt with full tracing
VNCVT_LOG_RFB=1 uv run python -m vncvt \
    --port 5900 --password testpass --log-traffic \
    2>/tmp/vncvt-local.log

# Terminal 2 — watch the log live
tail -f /tmp/vncvt-local.log

# Terminal 3 — capture the wire for second opinion
sudo tcpdump -i lo0 -X -s 0 'port 5900' > /tmp/rfb-wire.txt

# Terminal 4 — launch Screen Sharing
open "vnc://testpass@127.0.0.1:5900"
```

**Expected outcome if `8493ced` fixed the bug**: Screen Sharing opens
a remote desktop window showing the vncvt terminal, and the log shows
a complete handshake ending in `RFB: msg type=3` (FramebufferUpdateRequest)
and a large `>>` framebuffer update write.

**Expected outcome if the bug is deeper**: the log stops at some
specific point — version exchange, security type selection, auth
challenge, ClientInit, or somewhere in the message loop. That's the
new symptom you're debugging.

### Step 2: If Screen Sharing now works, build the calibration corpus

The previous session's `tests/check_scene_match.py` uses literature
defaults for SSIM and Bhattacharyya thresholds (`--ssim-min 0.70`,
`--bhat-max 0.50` for macOS). These are defensible but not calibrated
against our actual render pipeline.

**Task**: build `tests/scene_calibration/` with:

- ≥ 10 **same-content pairs** — Screen Sharing captures of terminal
  frames the server simultaneously dumped. Include variety: empty
  prompt, single line, 4-line login banner, full-screen `ls -la /usr`,
  htop, vim.
- ≥ 10 **cross-content pairs** — server's terminal frame vs. something
  totally different. Easy ones: the Connecting-dialog fallback
  capture, a Safari window, solid black, solid amber, a different
  terminal frame from the same session.

Run `tests/check_scene_match.py` against each pair, record SSIM and
Bhattacharyya distances, produce two distributions, and set thresholds
at the percentile-based bounds the research agent recommended:

- `--ssim-min` = min(SSIM_same) − 0.05, or 1st percentile of
  SSIM_same
- `--bhat-max` = 99th percentile of Bhat_same, or max(Bhat_same) +
  0.05

Commit the calibration corpus as a zip or individual PNGs under
`tests/scene_calibration/`, update the defaults in
`tests/check_scene_match.py` to the calibrated values, and add a
pytest-level test that recomputes metrics over the corpus and asserts
the gap between same and cross distributions is still there. That
gives future threshold tweaks a safety net.

### Step 3: Tighten the macOS job

Once Screen Sharing works reliably:

1. **Remove `continue-on-error: true`** from the `macos-screen-sharing`
   job (line 104 of `.github/workflows/loopback-test.yml`). This safety
   net was added because Screen Sharing was flaky; once it's not, it
   stops being a safety net and starts being a lie.
2. **Commit `tests/baselines/baselines-macos.json`** with the dHash of
   a known-good Screen Sharing capture so
   `tests/crop_and_hash_macos.py` stops hitting its "first run" path
   on every CI run.
3. **Consider removing `|| true` from the `screencapture` fallbacks**
   in the workflow. Once we know Screen Sharing creates a real window,
   a `screencapture -l <windowId>` failure becomes a genuine error
   worth reporting.

## Known timing race in the Linux vncdo job

The previous session added `Verify vncdo client matches server
framebuffer` as a step on the linux-vncdo-cross-check job, using tight
thresholds (`--ssim-min 0.90 --bhat-max 0.25`). This has a suspected
timing race the previous session didn't hit in local smoke tests
because it used `sleep 0.5` between vncdo's `capture` call and the
server-side dump. The CI step does not.

**If the Linux vncdo job is failing the new check in CI**, the fix is
to reorder the step so the server dump happens *after* vncdo has
executed the typed command but *before* vncdo's `capture` call:

```yaml
# current (flawed)
vncdo ... type "..." key enter capture /tmp/vncdo_cross.png   # capture first
echo '{"op":"dump",...}' | nc -U ...                           # dump second

# fixed
vncdo ... type "..." key enter             # execute, no capture
sleep 0.5                                   # let bash paint
echo '{"op":"dump",...}' | nc -U ...        # dump stable state
vncdo -s 127.0.0.1::5995 capture /tmp/vncdo_cross.png   # THEN capture
```

## File map (only the files you'll touch)

| Path | Role |
|------|------|
| `vncvt/server.py` | RFB server. Handshake at `_handshake()` around line 340, message loop at `_message_loop()` around line 400. `_trace()` helper at top, gated on `VNCVT_LOG_RFB=1`. |
| `vncvt/scene_dump.py` | Server-side scene bundle writer + Unix-domain control socket. JSON request/response protocol. |
| `vncvt/__main__.py` | CLI entry point. `--log-traffic`, `--password`, `--scene-dump-dir`, `--scene-control-socket`. |
| `tests/check_scene_match.py` | **The important new file**. SSIM + HSV Bhattacharyya + blank guard. Replace the literature-default thresholds with corpus-calibrated ones here. |
| `tests/crop_and_hash_macos.py` | Orthogonal drift gate. Keep it — it tests a different property. |
| `tests/scenes.py` | Test-side scene fixture (JSON client for the control socket). |
| `tests/test_vnc_auth.py` | 4 tests, including the `test_password_offers_only_vnc_auth` regression guard. |
| `.github/workflows/loopback-test.yml` | 3 jobs: `linux-pytest`, `linux-vncdo-cross-check`, `macos-screen-sharing`. |

## Useful commands

```
# Full local suite
uv run pytest tests/ -v

# Start vncvt with full RFB tracing (byte-level hex + message labels)
VNCVT_LOG_RFB=1 uv run python -m vncvt \
    --port 5900 --password testpass --log-traffic 2>/tmp/vncvt.log

# Trigger a scene dump via the control socket
echo '{"version":1,"op":"dump","name":"my_scene"}' \
    | nc -U /tmp/vncvt-scene.sock

# Verify a scene bundle
uv run --with scikit-image --with pillow \
    python tests/check_scene_match.py /tmp/vncvt-scenes/my_scene \
    --ssim-min 0.70 --bhat-max 0.50

# Check the failing CI run (assuming gh is available in your env)
gh run list --branch claude/vnc-terminal-server-YAmN5 --limit 5
gh run view <run-id> --log-failed
gh run download <run-id> -n scene-dumps-macos
```

## Environment notes

- **No `gh` CLI in the previous session**. If you have it, you
  dramatically outpace the previous session's diagnostic loop — the
  previous session was limited to reading artifacts that the user
  pasted by hand.
- **Stop hook**: `~/.claude/stop-hook-git-check.sh` blocks ending the
  session with uncommitted changes. Commit and push before ending
  turns that produced changes, even minor ones.
- **Never push to a branch other than `claude/vnc-terminal-server-YAmN5`**
  without explicit permission — that's the branch instruction
  inherited from the task prompt.
- **Scikit-image is not in the project dep set**. CI installs it
  ephemerally via `uv run --with scikit-image --with pillow`. Local
  runs of `tests/check_scene_match.py` need the same invocation.

## What to commit first in your session

Before doing anything else, consider appending a short
"session-2026-04-14" (or whatever date) section to this file
summarizing what you learned and pushed. Keeping each day's handoff
local to a dated section makes the history of this problem readable
for whoever picks it up next.
