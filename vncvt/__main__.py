"""Entry point for vncvt — VNC terminal server with VT220 amber aesthetic."""

import argparse
import asyncio
import importlib.metadata
import json
import logging
import os
import signal
import sys
from pathlib import Path

from .terminal import Terminal
from .renderer import TerminalRenderer, FONT_SEARCH_PATHS, _find_font
from .server import RFBServer
from .scene_dump import SceneDumper, serve_control_socket


def main() -> None:
    parser = argparse.ArgumentParser(
        description="VNC terminal server with VT220 amber aesthetic"
    )
    parser.add_argument(
        "--port", type=int, default=5900, help="VNC port (default: 5900)"
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="Listen address (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--cols", type=int, default=None,
        help="Terminal columns (default: 80; mutually exclusive with --mode)",
    )
    parser.add_argument(
        "--rows", type=int, default=None,
        help="Terminal rows (default: 24; mutually exclusive with --mode)",
    )
    parser.add_argument(
        "--mode", default=None, choices=("80x24", "132x24"),
        help="VT220 screen preset. Mutually exclusive with --cols/--rows.",
    )
    parser.add_argument(
        "--font-size", type=int, default=11, help="Font size in points (default: 11)"
    )
    parser.add_argument(
        "--fps", type=int, default=30,
        help="Framebuffer update rate cap (default: 30, range: 1-120)",
    )
    parser.add_argument(
        "--font", default=None, help="Path to a monospace TTF font"
    )
    parser.add_argument(
        "--shell",
        default=os.environ.get("SHELL") or "/bin/bash",
        help="Shell to run (default: $SHELL from the user's profile, "
             "falling back to /bin/bash)",
    )
    parser.add_argument(
        "--password", default=None,
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
            },
            "font_search_path": list(FONT_SEARCH_PATHS),
            "font_found": args.font or _find_font(FONT_SEARCH_PATHS),
            "listen": f"{args.host}:{args.port}",
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
    )
    server = RFBServer(
        host=args.host,
        port=args.port,
        terminal=terminal,
        renderer=renderer,
        password=args.password,
        log_traffic=args.log_traffic,
        fps=args.fps,
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
            logging.info("Shell process exited")
            server.shutdown()
            loop.stop()

    loop.add_signal_handler(signal.SIGCHLD, _on_sigchld)

    try:
        loop.run_until_complete(server.start())
    except KeyboardInterrupt:
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
