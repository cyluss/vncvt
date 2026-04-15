"""Entry point for vncvt — VNC terminal server with VT220 amber aesthetic."""

import argparse
import asyncio
import importlib.metadata
import json
import logging
import os
import signal
import sys
import tomllib
from pathlib import Path

from .terminal import Terminal
from .renderer import (
    TerminalRenderer, FONT_SEARCH_PATHS, _find_font, THEMES, apply_theme,
)
from .server import RFBServer
from .scene_dump import SceneDumper, serve_control_socket


# Config keys that are allowed in the TOML file. Each maps to the
# corresponding argparse `dest` name. We constrain the set so an
# unknown key in the config file fails loud.
_CONFIG_KEYS = {
    "host", "port", "cols", "rows", "mode", "font_size", "font",
    "fps", "theme", "line_height", "contrast", "shell", "password",
}


def _config_path() -> Path:
    """Return the XDG config path for vncvt."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "vncvt" / "config.toml"


def _load_config(path: Path) -> dict:
    """Load TOML config. Returns {} if the file doesn't exist.

    Raises SystemExit on invalid TOML or unknown keys so misconfiguration
    fails fast instead of silently ignoring the user's intent.
    """
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"vncvt: invalid config at {path}: {e}")
    # Normalize TOML key style (kebab-case → snake_case) so users can
    # write `font-size = 13` like the CLI flag.
    normalized = {k.replace("-", "_"): v for k, v in data.items()}
    unknown = set(normalized) - _CONFIG_KEYS
    if unknown:
        sys.exit(
            f"vncvt: unknown keys in {path}: {sorted(unknown)}. "
            f"Allowed: {sorted(_CONFIG_KEYS)}"
        )
    return normalized


def main() -> None:
    # First-pass parser to capture --no-config / --config so we know
    # whether to load the TOML file before building the real parser.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--no-config", action="store_true")
    pre.add_argument("--config", default=None)
    pre_args, _ = pre.parse_known_args()

    if pre_args.no_config:
        config = {}
    else:
        config_path = (
            Path(pre_args.config) if pre_args.config else _config_path()
        )
        config = _load_config(config_path)

    def cfg(key: str, fallback):
        return config.get(key, fallback)

    parser = argparse.ArgumentParser(
        description="VNC terminal server with VT220 amber aesthetic"
    )
    parser.add_argument(
        "--config", default=None,
        help=f"Path to a TOML config file (default: {_config_path()})",
    )
    parser.add_argument(
        "--no-config", action="store_true",
        help="Skip loading the config file even if one exists.",
    )
    parser.add_argument(
        "--port", type=int, default=cfg("port", 5900),
        help="VNC port (default: 5900)",
    )
    parser.add_argument(
        "--host", default=cfg("host", "127.0.0.1"),
        help="Listen address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--cols", type=int, default=cfg("cols", None),
        help="Terminal columns (default: 80; mutually exclusive with --mode)",
    )
    parser.add_argument(
        "--rows", type=int, default=cfg("rows", None),
        help="Terminal rows (default: 24; mutually exclusive with --mode)",
    )
    parser.add_argument(
        "--mode", default=cfg("mode", None), choices=("80x24", "132x24"),
        help="VT220 screen preset. Mutually exclusive with --cols/--rows.",
    )
    parser.add_argument(
        "--font-size", type=int, default=cfg("font_size", 13),
        help="Font size in points (default: 13). Glyphs are rasterized "
             "via Skia with subpixel AA + LCD filtering; 13pt SF Mono "
             "matches macOS Terminal.app's appearance.",
    )
    parser.add_argument(
        "--theme", default=cfg("theme", "light"), choices=sorted(THEMES),
        help="Color palette (default: light). light = black on white; "
             "dark = white on black; amber = amber phosphor on black; "
             "green = classic phosphor green.",
    )
    parser.add_argument(
        "--line-height", type=float, default=cfg("line_height", 1.1),
        help="Line height multiplier (default: 1.1). Values >1.0 add "
             "vertical space between rows; range 0.8-2.0.",
    )
    parser.add_argument(
        "--contrast", default=cfg("contrast", "high"),
        choices=("normal", "high", "max"),
        help="Glyph stroke boldness: normal (kFull hinting, single "
             "draw), high (kNone hinting + 1px horizontal embolden, "
             "~2x ink coverage, DEFAULT), max (kNone + 4-corner "
             "embolden, ~4x ink). Drop to normal if text looks too "
             "heavy.",
    )
    parser.add_argument(
        "--fps", type=int, default=cfg("fps", 15),
        help="Framebuffer update rate cap (default: 15, range: 1-120)",
    )
    parser.add_argument(
        "--font", default=cfg("font", None),
        help="Path to a monospace TTF font",
    )
    parser.add_argument(
        "--shell",
        default=cfg("shell", os.environ.get("SHELL") or "/bin/bash"),
        help="Shell to run (default: $SHELL from the user's profile, "
             "falling back to /bin/bash)",
    )
    parser.add_argument(
        "--password", default=cfg("password", None),
        help="Enable VNC Authentication (type 2) with this password. "
             "Required for macOS Screen Sharing.app. If omitted, only "
             "None auth (type 1) is offered.",
    )
    parser.add_argument(
        "--log-traffic", action="store_true",
        help="Log every byte of RFB traffic as hex (noisy — for debugging).",
    )
    parser.add_argument(
        "--scene-dump-dir", default=None,
        help="If set, enable per-scene debug dumps and write them under "
             "this directory. Requires --scene-control-socket.",
    )
    parser.add_argument(
        "--scene-control-socket", default=None,
        help="If set, open a Unix-domain-socket listener at this path. "
             "Clients send one JSON document per line: "
             "{\"version\":1,\"op\":\"dump\",\"name\":\"<name>\"} to "
             "trigger a scene dump. Requires --scene-dump-dir.",
    )
    parser.add_argument(
        "--info", action="store_true",
        help="Print a JSON diagnostic dump (version, defaults, font search path) "
             "and exit without starting the server.",
    )
    args = parser.parse_args()

    # Validate --fps
    if not 1 <= args.fps <= 120:
        parser.error("--fps must be between 1 and 120")

    # Validate --line-height
    if not 0.8 <= args.line_height <= 2.0:
        parser.error("--line-height must be between 0.8 and 2.0")

    # Install theme palette before constructing the renderer
    apply_theme(args.theme)

    # Resolve --mode preset
    if args.mode is not None:
        if args.cols is not None or args.rows is not None:
            parser.error("--mode is mutually exclusive with --cols/--rows")
        mode_cols, mode_rows = args.mode.split("x")
        args.cols = int(mode_cols)
        args.rows = int(mode_rows)
    if args.cols is None:
        args.cols = 80
    if args.rows is None:
        args.rows = 24

    # --info one-shot diagnostic
    if args.info:
        try:
            version = importlib.metadata.version("vncvt")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        info = {
            "version": version,
            "defaults": {
                "host": args.host,
                "port": args.port,
                "cols": args.cols,
                "rows": args.rows,
                "font_size": args.font_size,
                "fps": args.fps,
                "shell": args.shell,
                "theme": args.theme,
                "line_height": args.line_height,
            },
            "font_search_path": list(FONT_SEARCH_PATHS),
            "font_found": args.font or _find_font(FONT_SEARCH_PATHS),
            "listen": f"{args.host}:{args.port}",
            "config_file": str(_config_path()) if not pre_args.no_config else None,
            "config_loaded": bool(config),
        }
        print(json.dumps(info, indent=2))
        return

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    terminal = Terminal(cols=args.cols, rows=args.rows, shell=args.shell)
    renderer = TerminalRenderer(
        cols=args.cols,
        rows=args.rows,
        font_path=args.font,
        font_size=args.font_size,
        line_height=args.line_height,
        contrast=args.contrast,
    )
    server = RFBServer(
        host=args.host,
        port=args.port,
        terminal=terminal,
        renderer=renderer,
        password=args.password,
        log_traffic=args.log_traffic,
        fps=args.fps,
        theme=args.theme,
    )

    if bool(args.scene_dump_dir) != bool(args.scene_control_socket):
        parser.error(
            "--scene-dump-dir and --scene-control-socket must be used together"
        )

    loop = asyncio.new_event_loop()

    scene_control_server = None
    if args.scene_dump_dir:
        dumper = SceneDumper(
            terminal=terminal,
            renderer=renderer,
            out_dir=Path(args.scene_dump_dir),
        )
        scene_control_server = loop.run_until_complete(
            serve_control_socket(
                dumper,
                Path(args.scene_control_socket),
                rfb_server=server,
            )
        )
        loop.create_task(scene_control_server.serve_forever())

    def _on_sigchld() -> None:
        if not terminal.alive():
            logging.info("Shell process exited — showing goodbye notice")
            # Schedule the graceful shutdown on the event loop; don't
            # call shutdown() directly here because we want the render
            # loop to push one more update before we tear down.
            async def _goodbye():
                await server.shutdown_with_notice(
                    "[ Shell exited. Press Ctrl+C on the server to quit. ]"
                )
                loop.stop()
            loop.create_task(_goodbye())

    loop.add_signal_handler(signal.SIGCHLD, _on_sigchld)

    try:
        loop.run_until_complete(server.start())
    except (KeyboardInterrupt, RuntimeError):
        # RuntimeError arises if the loop was stopped by _on_sigchld
        # (shell exit path) — server.start() was awaiting serve_forever()
        # and never got a chance to return.
        pass
    finally:
        if scene_control_server is not None:
            scene_control_server.close()
            try:
                Path(args.scene_control_socket).unlink(missing_ok=True)
            except Exception:
                pass
        terminal.close()
        loop.close()


if __name__ == "__main__":
    main()
