# Test strategy

How vncvt's test suite is organized and why.

## Principle: cost-proportional coverage

Every assertion costs test runtime. The suite uses a three-tier cost
model so cheap checks run on all themes while expensive rendering runs
only on representative themes.

| Tier | Cost | Scope | What it tests |
|---|---|---|---|
| **1. Metadata** | ~0.01s per theme | All 14 themes | Palette shape (256 entries), chroma floor, hue invariant, WCAG floor. No rendering — just `apply_theme` + sample palette entries. |
| **2. Logic** | ~0.1s per case | 3 representative themes | Renderer dispatch, contrast floor, pole orientation, truecolor hex tinting. Uses `TerminalRenderer._resolve_color` or a single `replay_cast` call. |
| **3. Replay** | ~0.5s per case | Default theme only (amber) | Full `.cast` replay + per-cell WCAG inspect. Proves the end-to-end pipeline from PTY bytes through pyte through the renderer to pixel-level contrast. |

### Why 3 representative themes?

The renderer's color pipeline has three distinct code paths that
depend on theme properties:

| Representative | Property | Why it matters |
|---|---|---|
| `amber` | Warm hue, dark bg, high chroma fg | The default — catches regressions in the most common path. |
| `cyan` | Cool hue, dark bg, low-ish chroma fg | Covers the GTIA pastel hue family; catches ramp direction bugs for cool-biased themes. |
| `light` | Inverted polarity (bright bg, dark fg) | The only theme where `bg_L > fg_L`. Catches pole-orientation bugs that assume fg is always brighter. |

Together these three cover: warm/cool hue, dark/light bg, high/low
chroma, normal/inverted polarity. If a renderer change passes all
three, it'll pass the other 11 themes too — their code paths are
subsets of these three.

## Test file layout

```
tests/
├── unit/
│   ├── test_color_mode.py      — tiers 1+2+3 for color/phosphor
│   ├── test_cast_replay.py     — cast loading, frame replay, row-0 regression
│   ├── test_setup_screen.py    — SET-UP mode snapshot, field cycling
│   ├── test_screenshot_freshness.py — CI guard: committed screenshots match renderer
│   └── test_padding.py         — renderer padding, cell geometry
├── integration/
│   ├── test_cli.py             — --info JSON, port binding
│   ├── test_setup_cycling.py   — full SET-UP apply + persistence
│   └── test_unicode_fallback.py — CJK + emoji font fallback
├── e2e/
│   ├── test_loopback.py        — RFB handshake, vncdotool cross-check
│   └── test_claude_code.py     — Claude Code welcome panel, theme switch
└── fixtures/
    └── claude-light-row0-invisible.cast — captured Claude session
```

## Screenshot freshness guard

`test_screenshot_freshness.py` catches the scenario where a renderer
fix lands without matching screenshot regeneration. It:

1. Opens each committed `docs/screenshots/claude-code-<theme>.png`.
2. Asserts avg pixel chroma >= 15 for chromatic themes.
3. Replays the `.cast` fixture through the current renderer and
   compares chroma — if the renderer produces high chroma but the
   screenshot has low chroma, the screenshot is stale.

Error messages include the exact regeneration command:
```
uv run python scripts/refresh_showcase.py <theme>
```

## Adding a new theme

When you add a theme to `THEMES` in `vncvt/theme.py`:

1. **Tier 1 tests auto-expand** — they parametrize from `sorted(THEMES)`.
2. **Tier 2 tests don't change** — they use the 3 fixed representatives.
3. **Add a showcase.toml entry** — so `refresh_showcase.py` captures it.
4. **Run `refresh_showcase.py <theme>`** — generates the screenshot.
5. **Screenshot freshness auto-expands** — it auto-detects chromatic
   themes via `srgb_to_oklch`.

No test file needs manual editing.

## Running

```bash
uv run pytest tests/ -q                    # full suite
uv run pytest tests/unit/ -q               # fast: metadata + logic only
uv run pytest tests/ -m claude_code        # e2e Claude Code tests (needs claude binary)
uv run pytest tests/ -k "not replay"       # skip expensive replay tests
```
