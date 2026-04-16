"""X11 keysym-to-bytes translation for terminal PTY input."""

_SPECIAL: dict[int, bytes] = {
    0xFF08: b"\x7f",       # BackSpace
    0xFF09: b"\t",         # Tab
    0xFF0D: b"\r",         # Return
    0xFF0A: b"\r",         # Linefeed (some clients)
    0xFF1B: b"\x1b",       # Escape
    0xFFFF: b"\x1b[3~",    # Delete
    0xFF63: b"\x1b[2~",    # Insert
    0xFF50: b"\x1b[H",     # Home
    0xFF57: b"\x1b[F",     # End
    0xFF55: b"\x1b[5~",    # Page_Up
    0xFF56: b"\x1b[6~",    # Page_Down
    0xFF51: b"\x1b[D",     # Left
    0xFF52: b"\x1b[A",     # Up
    0xFF53: b"\x1b[C",     # Right
    0xFF54: b"\x1b[B",     # Down
    # F1-F12
    0xFFBE: b"\x1bOP",     0xFFBF: b"\x1bOQ",
    0xFFC0: b"\x1bOR",     0xFFC1: b"\x1bOS",
    0xFFC2: b"\x1b[15~",   0xFFC3: b"\x1b[17~",
    0xFFC4: b"\x1b[18~",   0xFFC5: b"\x1b[19~",
    0xFFC6: b"\x1b[20~",   0xFFC7: b"\x1b[21~",
    0xFFC8: b"\x1b[23~",   0xFFC9: b"\x1b[24~",
}


def keysym_to_bytes(
    keysym: int, ctrl_pressed: bool, alt_pressed: bool = False,
) -> bytes | None:
    """Convert an X11 keysym to the byte sequence to write to a terminal PTY."""
    # Modifier keys produce no output
    if 0xFFE1 <= keysym <= 0xFFEE:
        return None

    # Ctrl+letter
    if ctrl_pressed:
        if 0x61 <= keysym <= 0x7A:  # a-z
            return bytes([keysym - 0x60])
        if 0x41 <= keysym <= 0x5A:  # A-Z
            return bytes([keysym - 0x40])
        # Ctrl+[ = Escape, Ctrl+\ = 0x1c, etc.
        if keysym in (0x5B, 0x5C, 0x5D, 0x5E, 0x5F):
            return bytes([keysym - 0x40])

    # Shift+Tab → ISO_Left_Tab (keysym 0xFE20 sent by most VNC clients
    # including macOS Screen Sharing) → CSI Z escape, per xterm.
    if keysym == 0xFE20:
        return b"\x1b[Z"

    # Special keys
    base: bytes | None = None
    if 0x20 <= keysym <= 0x7E:
        base = bytes([keysym])
    elif keysym in _SPECIAL:
        base = _SPECIAL[keysym]
    elif keysym >= 0x01000000:  # Unicode keysym
        base = chr(keysym - 0x01000000).encode("utf-8")
    elif 0x00A0 <= keysym <= 0x00FF:  # Latin-1
        base = chr(keysym).encode("utf-8")

    if base is None:
        return None

    # Meta/Alt prefix: standard xterm encoding is ESC + <key>.
    # Readline, bash, zsh, and Claude Code all parse this form.
    if alt_pressed:
        return b"\x1b" + base

    return base
