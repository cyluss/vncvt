"""Committed perceptual-hash baselines for visual regression.

Baselines live in `tests/baselines/baselines.json` as a flat
`{name: hex_dhash}` dict. Helpers read/write this file and compare
`imagehash.ImageHash` instances.
"""

from __future__ import annotations

import json
from pathlib import Path

import imagehash

_BASELINE_FILE = Path(__file__).parent / "baselines" / "baselines.json"


def _load_all() -> dict[str, str]:
    if not _BASELINE_FILE.exists():
        return {}
    return json.loads(_BASELINE_FILE.read_text())


def _dump_all(data: dict[str, str]) -> None:
    _BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _BASELINE_FILE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def load_hash(name: str) -> imagehash.ImageHash | None:
    """Return the stored dHash for `name`, or None if missing."""
    data = _load_all()
    hex_str = data.get(name)
    if hex_str is None:
        return None
    return imagehash.hex_to_hash(hex_str)


def save_hash(name: str, h: imagehash.ImageHash) -> None:
    """Persist `h` as the new baseline for `name`."""
    data = _load_all()
    data[name] = str(h)
    _dump_all(data)


def assert_hash(
    name: str,
    current: imagehash.ImageHash,
    tolerance: int,
    update: bool,
) -> None:
    """Assert that `current` is close to the baseline, or update it.

    If `update=True`, overwrite the baseline (for `--update-baselines`).
    Otherwise assert hamming distance <= tolerance. If there's no
    baseline yet, save the current value and pass (first run).
    """
    baseline = load_hash(name)
    if update or baseline is None:
        save_hash(name, current)
        return
    distance = current - baseline
    assert distance <= tolerance, (
        f"dHash distance {distance} > {tolerance} for {name!r} "
        f"(baseline {baseline}, got {current})"
    )
