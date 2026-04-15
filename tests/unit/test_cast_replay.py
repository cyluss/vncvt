"""Tests for vncvt.cast_replay: cast loading, frame replay, and the
row-0-invisible regression fixture captured from a Claude light-theme
session."""

from __future__ import annotations

from pathlib import Path

import pytest

from vncvt.cast_replay import (
    ReplayedFrame,
    inspect_frame,
    load_cast,
    replay_cast,
    replay_frames,
)


FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures"
    / "claude-light-row0-invisible.cast"
)


def test_load_cast_smoke():
    cast = load_cast(FIXTURE)
    assert cast.cols == 80
    assert cast.rows == 24
    assert cast.duration > 0
    assert len(cast.events) > 10
    for ev in cast.events:
        assert ev.kind in ("o", "i", "r")


def test_replay_frames_yields_n():
    timestamps = [0.5, 1.0, 2.0]
    frames = list(replay_frames(FIXTURE, timestamps=timestamps, theme="dark"))
    assert len(frames) == 3
    for frame, ts in zip(frames, timestamps):
        assert isinstance(frame, ReplayedFrame)
        assert frame.at == ts


@pytest.mark.parametrize(
    "theme", ["light", "dark", "amber", "green", "powershell"]
)
def test_claude_row0_readable(theme):
    frame = replay_cast(FIXTURE, theme=theme)
    report = inspect_frame(frame, threshold=3.0)
    invisible = [
        c for c in report.below_threshold
        if c.char not in (" ", "\xa0")
    ]
    assert not invisible, (
        f"{theme}: {len(invisible)} invisible non-ws cells, "
        f"worst: {invisible[:3]}"
    )
