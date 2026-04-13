"""Entry point for vncvt — VNC terminal server with VT220 amber aesthetic."""

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from .terminal import Terminal
from .renderer import TerminalRenderer
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
        "--cols", type=int, default=80, help="Terminal columns (default: 80)"
    )
    parser.add_argument(
        "--rows", type=int, default=24, help="Terminal rows (default: 24)"
    )
    parser.add_argument(
        "--font-size", type=int, default=16, help="Font size in points (default: 16)"
    )
    parser.add_argument(
        "--font", default=None, help="Path to a monospace TTF font"
    )
    parser.add_argument(
        "--shell", default="/bin/bash", help="Shell to run (default: /bin/bash)"
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
             "Tests send 'DUMP <name>\\n' to trigger a scene dump. "
             "Requires --scene-dump-dir.",
    )
    args = parser.parse_args()

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
            serve_control_socket(dumper, Path(args.scene_control_socket))
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
