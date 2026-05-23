"""Raw-protocol test: VNC Authentication (RFB security type 2).

Password is mandatory (default: "vncvt"). Verifies the DES
challenge/response path. Happy path must succeed with the right
password; wrong password must be rejected with SecurityResult=1 and
an RFB 3.8 reason string.

Both branches are byte-level because a high-level client would
abstract away the failure mode we want to observe (the reason string
and the server-initiated disconnect).
"""

from __future__ import annotations

import socket
import struct

import pytest

from vncvt.server import _vnc_encrypt


def _handshake_version(s: socket.socket, minor: int = 8) -> None:
    assert s.recv(12) == b"RFB 003.008\n"
    s.send(f"RFB 003.{minor:03d}\n".encode("ascii"))


def test_vnc_auth_accepts_correct_password(vncvt_server_factory):
    host, port, _ = vncvt_server_factory("--password", "hunter2")
    s = socket.create_connection((host, port), timeout=5.0)
    try:
        _handshake_version(s)

        n_sec = s.recv(1)[0]
        sec_types = list(s.recv(n_sec))
        assert 2 in sec_types, f"expected VNC auth in offered types, got {sec_types}"

        # Pick VNC auth.
        s.send(bytes([2]))

        challenge = s.recv(16)
        assert len(challenge) == 16
        s.send(_vnc_encrypt(challenge, "hunter2"))

        # SecurityResult: u32 big-endian; 0 = OK.
        result = struct.unpack(">I", s.recv(4))[0]
        assert result == 0, f"expected SecurityResult=0, got {result}"

        # Handshake should continue; send ClientInit and read ServerInit.
        s.send(bytes([1]))  # shared flag
        server_init = s.recv(24)
        w, h = struct.unpack(">HH", server_init[0:4])
        assert w > 0 and h > 0
    finally:
        s.close()


def test_vnc_auth_rejects_wrong_password(vncvt_server_factory):
    host, port, _ = vncvt_server_factory("--password", "hunter2")
    s = socket.create_connection((host, port), timeout=5.0)
    try:
        _handshake_version(s)
        n_sec = s.recv(1)[0]
        s.recv(n_sec)
        s.send(bytes([2]))

        challenge = s.recv(16)
        # Encrypt with a wrong password.
        s.send(_vnc_encrypt(challenge, "wrong_password"))

        result = struct.unpack(">I", s.recv(4))[0]
        assert result == 1, f"expected SecurityResult=1 (failed), got {result}"

        # RFB 3.8: failure is followed by a reason string (u32 length + text).
        reason_len = struct.unpack(">I", s.recv(4))[0]
        reason = s.recv(reason_len).decode("latin-1")
        assert "fail" in reason.lower() or "auth" in reason.lower(), (
            f"unexpected reason string: {reason!r}"
        )

        # Server should close the connection; subsequent reads return b"".
        tail = s.recv(1)
        assert tail == b"", "expected server to close connection after failed auth"
    finally:
        s.close()


def test_default_password_accepts_vncvt(vncvt_server):
    """Server started without explicit --password uses the default
    password 'vncvt' and must accept VNC auth with it."""
    host, port = vncvt_server
    s = socket.create_connection((host, port), timeout=5.0)
    try:
        _handshake_version(s)
        n_sec = s.recv(1)[0]
        sec_types = list(s.recv(n_sec))
        assert sec_types == [2], f"expected [VNC=2], got {sec_types}"
        s.send(bytes([2]))

        challenge = s.recv(16)
        assert len(challenge) == 16
        s.send(_vnc_encrypt(challenge, "vncvt"))

        result = struct.unpack(">I", s.recv(4))[0]
        assert result == 0, f"expected SecurityResult=0, got {result}"
    finally:
        s.close()


def test_rfb_33_vnc_auth_uses_uint32_security_type(vncvt_server_factory):
    """RFB 3.3 §7.1.2: server sends a single u32 security-type value
    (not a count-prefixed list). Apple's Screen Sharing.app on
    macOS speaks 3.3, so this is the path that has to work for the
    macos-screen-sharing CI job to succeed.
    """
    host, port, _ = vncvt_server_factory("--password", "hunter2")
    s = socket.create_connection((host, port), timeout=5.0)
    try:
        _handshake_version(s, minor=3)

        sec_type = struct.unpack(">I", s.recv(4))[0]
        assert sec_type == 2, f"expected u32 VNC auth (2), got {sec_type}"

        challenge = s.recv(16)
        assert len(challenge) == 16
        s.send(_vnc_encrypt(challenge, "hunter2"))

        result = struct.unpack(">I", s.recv(4))[0]
        assert result == 0, f"expected SecurityResult=0, got {result}"

        s.send(bytes([1]))  # ClientInit shared flag
        server_init = s.recv(24)
        w, h = struct.unpack(">HH", server_init[0:4])
        assert w > 0 and h > 0
    finally:
        s.close()


def test_rfb_33_default_password_vnc_auth(vncvt_server):
    """RFB 3.3 with the default password must negotiate VNC auth
    (type 2) via the u32 security-type path."""
    host, port = vncvt_server
    s = socket.create_connection((host, port), timeout=5.0)
    try:
        _handshake_version(s, minor=3)

        sec_type = struct.unpack(">I", s.recv(4))[0]
        assert sec_type == 2, f"expected u32 VNC auth (2), got {sec_type}"

        challenge = s.recv(16)
        assert len(challenge) == 16
        s.send(_vnc_encrypt(challenge, "vncvt"))

        result = struct.unpack(">I", s.recv(4))[0]
        assert result == 0, f"expected SecurityResult=0, got {result}"

        s.send(bytes([1]))  # shared flag
        server_init = s.recv(24)
        w, h = struct.unpack(">HH", server_init[0:4])
        assert w > 0 and h > 0
    finally:
        s.close()


def test_rfb_37_failed_auth_omits_reason_string(vncvt_server_factory):
    """RFB 3.7 sends SecurityResult on auth failure but, unlike 3.8,
    does NOT follow it with a length-prefixed reason string. The
    server should just close the connection after the u32.
    """
    host, port, _ = vncvt_server_factory("--password", "hunter2")
    s = socket.create_connection((host, port), timeout=5.0)
    try:
        _handshake_version(s, minor=7)

        n_sec = s.recv(1)[0]
        sec_types = list(s.recv(n_sec))
        assert sec_types == [2]
        s.send(bytes([2]))

        challenge = s.recv(16)
        s.send(_vnc_encrypt(challenge, "wrong_password"))

        result = struct.unpack(">I", s.recv(4))[0]
        assert result == 1, f"expected SecurityResult=1, got {result}"

        # No reason string in 3.7 — server must close immediately.
        tail = s.recv(4)
        assert tail == b"", (
            f"expected immediate close after 3.7 SecurityResult, got {tail!r}"
        )
    finally:
        s.close()


def test_password_offers_only_vnc_auth(vncvt_server_factory):
    """When ``--password`` is set, the server must advertise VNC auth
    (2) and MUST NOT also advertise None (1). Offering both would
    allow any client to bypass the password entirely by selecting
    None, and has been observed to hang Apple's Screen Sharing.app
    during the security-type negotiation step.
    """
    host, port, _ = vncvt_server_factory("--password", "hunter2")
    s = socket.create_connection((host, port), timeout=5.0)
    try:
        _handshake_version(s)
        n_sec = s.recv(1)[0]
        assert n_sec == 1, (
            f"password-protected server must offer exactly 1 security "
            f"type, got {n_sec}"
        )
        sec_types = list(s.recv(n_sec))
        assert sec_types == [2], (
            f"password-protected server must offer ONLY VNC auth "
            f"(type 2). Got {sec_types}. Offering None (1) alongside "
            f"VNC auth is a password-bypass hole."
        )
    finally:
        s.close()
