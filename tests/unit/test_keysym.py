"""Unit tests for keysym_to_bytes() — the X11 keysym-to-PTY-bytes translator.

Imports directly from vncvt.keysym (no heavy dependencies).
"""

import pytest

from vncvt.keysym import keysym_to_bytes


# ---------------------------------------------------------------------------
# 1. ASCII printable range: keysyms 0x20–0x7e map to their byte values
# ---------------------------------------------------------------------------

class TestASCIIPrintable:
    """keysyms 0x20-0x7e should produce the corresponding single byte."""

    def test_space(self):
        assert keysym_to_bytes(0x20, ctrl_pressed=False) == b" "

    def test_exclamation(self):
        assert keysym_to_bytes(0x21, ctrl_pressed=False) == b"!"

    def test_lowercase_a(self):
        assert keysym_to_bytes(ord("a"), ctrl_pressed=False) == b"a"

    def test_uppercase_A(self):
        assert keysym_to_bytes(ord("A"), ctrl_pressed=False) == b"A"

    def test_digit_0(self):
        assert keysym_to_bytes(ord("0"), ctrl_pressed=False) == b"0"

    def test_tilde(self):
        assert keysym_to_bytes(0x7E, ctrl_pressed=False) == b"~"

    @pytest.mark.parametrize("code", range(0x20, 0x7F))
    def test_all_printable(self, code):
        result = keysym_to_bytes(code, ctrl_pressed=False)
        assert result == bytes([code])


# ---------------------------------------------------------------------------
# 2. Ctrl+letter combos → 0x01–0x1a
# ---------------------------------------------------------------------------

class TestCtrlLetter:
    @pytest.mark.parametrize(
        "letter,expected",
        [(chr(ord("a") + i), bytes([i + 1])) for i in range(26)],
    )
    def test_ctrl_lowercase(self, letter, expected):
        assert keysym_to_bytes(ord(letter), ctrl_pressed=True) == expected

    @pytest.mark.parametrize(
        "letter,expected",
        [(chr(ord("A") + i), bytes([i + 1])) for i in range(26)],
    )
    def test_ctrl_uppercase(self, letter, expected):
        assert keysym_to_bytes(ord(letter), ctrl_pressed=True) == expected


# ---------------------------------------------------------------------------
# 3. Alt+letter → ESC prefix
# ---------------------------------------------------------------------------

class TestAltLetter:
    def test_alt_a(self):
        assert keysym_to_bytes(ord("a"), ctrl_pressed=False, alt_pressed=True) == b"\x1ba"

    def test_alt_z(self):
        assert keysym_to_bytes(ord("z"), ctrl_pressed=False, alt_pressed=True) == b"\x1bz"

    def test_alt_digit(self):
        assert keysym_to_bytes(ord("5"), ctrl_pressed=False, alt_pressed=True) == b"\x1b5"


# ---------------------------------------------------------------------------
# 4. Function keys F1–F12
# ---------------------------------------------------------------------------

_F_KEYS = {
    0xFFBE: b"\x1bOP",    # F1
    0xFFBF: b"\x1bOQ",    # F2
    0xFFC0: b"\x1bOR",    # F3
    0xFFC1: b"\x1bOS",    # F4
    0xFFC2: b"\x1b[15~",  # F5
    0xFFC3: b"\x1b[17~",  # F6
    0xFFC4: b"\x1b[18~",  # F7
    0xFFC5: b"\x1b[19~",  # F8
    0xFFC6: b"\x1b[20~",  # F9
    0xFFC7: b"\x1b[21~",  # F10
    0xFFC8: b"\x1b[23~",  # F11
    0xFFC9: b"\x1b[24~",  # F12
}

class TestFunctionKeys:
    @pytest.mark.parametrize("keysym,expected", list(_F_KEYS.items()))
    def test_function_key(self, keysym, expected):
        assert keysym_to_bytes(keysym, ctrl_pressed=False) == expected


# ---------------------------------------------------------------------------
# 5. Arrow keys
# ---------------------------------------------------------------------------

class TestArrowKeys:
    def test_up(self):
        assert keysym_to_bytes(0xFF52, ctrl_pressed=False) == b"\x1b[A"

    def test_down(self):
        assert keysym_to_bytes(0xFF54, ctrl_pressed=False) == b"\x1b[B"

    def test_right(self):
        assert keysym_to_bytes(0xFF53, ctrl_pressed=False) == b"\x1b[C"

    def test_left(self):
        assert keysym_to_bytes(0xFF51, ctrl_pressed=False) == b"\x1b[D"


# ---------------------------------------------------------------------------
# 6. Special keys
# ---------------------------------------------------------------------------

class TestSpecialKeys:
    def test_backspace(self):
        assert keysym_to_bytes(0xFF08, ctrl_pressed=False) == b"\x7f"

    def test_delete(self):
        assert keysym_to_bytes(0xFFFF, ctrl_pressed=False) == b"\x1b[3~"

    def test_escape(self):
        assert keysym_to_bytes(0xFF1B, ctrl_pressed=False) == b"\x1b"

    def test_return(self):
        assert keysym_to_bytes(0xFF0D, ctrl_pressed=False) == b"\r"

    def test_tab(self):
        assert keysym_to_bytes(0xFF09, ctrl_pressed=False) == b"\t"

    def test_insert(self):
        assert keysym_to_bytes(0xFF63, ctrl_pressed=False) == b"\x1b[2~"


# ---------------------------------------------------------------------------
# 7. Shift+Tab (ISO_Left_Tab)
# ---------------------------------------------------------------------------

class TestShiftTab:
    def test_iso_left_tab(self):
        assert keysym_to_bytes(0xFE20, ctrl_pressed=False) == b"\x1b[Z"


# ---------------------------------------------------------------------------
# 8. Navigation keys
# ---------------------------------------------------------------------------

class TestNavigation:
    def test_home(self):
        assert keysym_to_bytes(0xFF50, ctrl_pressed=False) == b"\x1b[H"

    def test_end(self):
        assert keysym_to_bytes(0xFF57, ctrl_pressed=False) == b"\x1b[F"

    def test_page_up(self):
        assert keysym_to_bytes(0xFF55, ctrl_pressed=False) == b"\x1b[5~"

    def test_page_down(self):
        assert keysym_to_bytes(0xFF56, ctrl_pressed=False) == b"\x1b[6~"


# ---------------------------------------------------------------------------
# 9. Unknown keysym → None
# ---------------------------------------------------------------------------

class TestUnknown:
    def test_unknown_keysym_returns_none(self):
        assert keysym_to_bytes(0xDEAD, ctrl_pressed=False) is None

    def test_modifier_keys_return_none(self):
        # Shift, Ctrl, Alt keysyms should not produce PTY bytes
        for keysym in (0xFFE1, 0xFFE3, 0xFFE9):
            assert keysym_to_bytes(keysym, ctrl_pressed=False) is None
