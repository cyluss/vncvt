"""Raw-protocol test: VNC Authentication (RFB security type 2).

Verifies the DES challenge/response path that vncvt offers when it's
started with ``--password``. Happy path must succeed with the right
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


def _handshake_version(s: socket.socket) -> None:
    assert s.recv(12) == b"RFB 003.008\n"
    s.send(b"RFB 003.008\n")


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


def test_no_password_offers_only_none_auth(vncvt_server):
    """When vncvt is started without --password, it must advertise
    exactly one security type (None = 1) for backwards compat with
    clients that don't support VNC auth."""
    host, port = vncvt_server
    s = socket.create_connection((host, port), timeout=5.0)
    try:
        _handshake_version(s)
        n_sec = s.recv(1)[0]
        assert n_sec == 1, f"expected 1 security type, got {n_sec}"
        sec_types = list(s.recv(n_sec))
        assert sec_types == [1], f"expected [None=1], got {sec_types}"
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
