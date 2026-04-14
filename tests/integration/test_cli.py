"""Phase 1 CLI flag tests: --fps, --mode, --info."""

from __future__ import annotations

import json
import socket
import subprocess
import sys

import pytest


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.2)
    try:
        s.connect((host, port))
        s.close()
        return True
    except (ConnectionRefusedError, OSError):
        return False


def test_fps_valid_accepted(vncvt_server_factory):
    """Spawn with --fps 60 and confirm the server starts."""
    host, port, proc = vncvt_server_factory("--fps", "60")
    assert proc.poll() is None, "server should still be running"
    assert _port_in_use(port), f"server should be listening on {port}"


def test_fps_out_of_range_rejected():
    """--fps 200 should fail with a clear error."""
    result = subprocess.run(
        [sys.executable, "-m", "vncvt", "--fps", "200"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "--fps must be between" in result.stderr


def test_fps_zero_rejected():
    """--fps 0 should fail (range is 1-120)."""
    result = subprocess.run(
        [sys.executable, "-m", "vncvt", "--fps", "0"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "--fps must be between" in result.stderr


def test_mode_132x24_accepted(vncvt_server_factory):
    """--mode 132x24 should start the server with 132-col geometry."""
    host, port, proc = vncvt_server_factory("--mode", "132x24")
    assert proc.poll() is None
    assert _port_in_use(port)


def test_mode_conflicts_with_cols():
    """--mode and --cols are mutually exclusive."""
    result = subprocess.run(
        [sys.executable, "-m", "vncvt", "--mode", "132x24", "--cols", "100"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "mutually exclusive" in result.stderr


def test_info_prints_json_and_exits():
    """--info prints a valid JSON diagnostic and exits without listening."""
    result = subprocess.run(
        [sys.executable, "-m", "vncvt", "--info"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    info = json.loads(result.stdout)
    assert "version" in info and info["version"]
    assert "defaults" in info
    assert info["defaults"]["cols"] == 80
    assert info["defaults"]["rows"] == 24
    assert info["defaults"]["fps"] == 30
    assert "font_search_path" in info and len(info["font_search_path"]) > 0
    assert "font_found" in info
    # Should not leave anything listening on the default port
    assert not _port_in_use(5900), "--info should not start the server"
