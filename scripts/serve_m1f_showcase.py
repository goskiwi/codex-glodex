"""Serve the static M1f showcase on loopback only."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final, Protocol

PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
SHOWCASE_ROOT: Final = PROJECT_ROOT / "showcase"
LOOPBACK_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8765


class ShowcaseServer(Protocol):
    """Minimal server seam so contract tests need not open a socket."""

    def serve_forever(self) -> None: ...

    def server_close(self) -> None: ...


class ShowcaseRequestHandler(SimpleHTTPRequestHandler):
    """Serve exactly the static showcase directory without request logging."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["directory"] = str(SHOWCASE_ROOT)
        super().__init__(*args, **kwargs)

    def log_message(self, _format: str, *_args: object) -> None:
        return None


type ServerFactory = Callable[[tuple[str, int], type[ShowcaseRequestHandler]], ShowcaseServer]


def build_server(
    port: int,
    *,
    server_factory: ServerFactory = ThreadingHTTPServer,
) -> ShowcaseServer:
    """Create one loopback-only server without starting its request loop."""

    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return server_factory((LOOPBACK_HOST, port), ShowcaseRequestHandler)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="serve the local M1f static showcase")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="loopback port from 1 to 65535 (default: 8765)",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    server_factory: ServerFactory = ThreadingHTTPServer,
) -> int:
    """Start the one permitted static local server until the operator stops it."""

    args = _build_parser().parse_args(argv)
    try:
        server = build_server(args.port, server_factory=server_factory)
    except ValueError:
        return 2
    print(f"Showcase: http://{LOOPBACK_HOST}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
