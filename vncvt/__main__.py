"""Entry point for vncvt — VNC terminal server with VT220 amber aesthetic."""

import argparse
import asyncio
import logging
import signal
import sys

from .terminal import Terminal
from .renderer import TerminalRenderer
from .server import RFBServer


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
    )

    loop = asyncio.new_event_loop()

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
        terminal.close()
        loop.close()


if __name__ == "__main__":
    main()
