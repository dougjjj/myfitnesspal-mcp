import argparse
import logging
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="myfitnesspal-mcp",
        description="MCP server for MyFitnessPal (stdio by default).",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["serve", "auth", "keepalive"],
        default="serve",
        help=(
            "serve (default), auth to connect your account, or keepalive to "
            "roll a live session before it expires"
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="with auth: report whether the saved session works, without prompting",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help=(
            "serve over streamable HTTP instead of stdio. Requires "
            "MFP_HTTP_TOKEN. Binds 127.0.0.1 unless MFP_HTTP_ALLOW_LAN=1"
        ),
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="HTTP bind host (default 127.0.0.1)"
    )
    parser.add_argument(
        "--port", type=int, default=8484, help="HTTP port (default 8484)"
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help=(
            "with keepalive: keep refreshing until the process is stopped. "
            "A failed refresh is logged and retried"
        ),
    )
    parser.add_argument(
        "--interval",
        default=None,
        help="with keepalive --loop: delay between refreshes (default 20m; 20m, 1h, 90s)",
    )
    parser.add_argument(
        "--max-failures",
        type=int,
        default=None,
        help=(
            "with keepalive --loop: print an alert after this many consecutive "
            "failures. The loop keeps running"
        ),
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.check and args.command != "auth":
        parser.error("--check only applies to the auth command")
    if args.command != "keepalive" and (
        args.loop or args.interval or args.max_failures is not None
    ):
        parser.error("--loop, --interval, and --max-failures apply to keepalive")
    if args.command == "keepalive" and args.interval and not args.loop:
        parser.error("--interval applies to keepalive --loop")
    if args.command == "keepalive" and args.max_failures is not None and not args.loop:
        parser.error("--max-failures applies to keepalive --loop")
    if (
        args.command == "keepalive"
        and args.max_failures is not None
        and args.max_failures < 1
    ):
        parser.error("--max-failures must be at least 1")
    if args.command == "keepalive" and args.http:
        parser.error("--http does not apply to keepalive")

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    if args.command == "keepalive":
        from .refresh import parse_interval, run_keepalive

        try:
            seconds = parse_interval(args.interval) if args.interval else None
        except ValueError as exc:
            parser.error(str(exc))
        raise SystemExit(
            run_keepalive(
                loop=args.loop,
                interval=seconds,
                max_failures=args.max_failures,
            )
        )

    if args.command == "auth" and args.check:
        from .auth import run_check

        raise SystemExit(run_check())

    if args.command == "auth":
        from .auth import run_auth_flow

        raise SystemExit(run_auth_flow())

    from .server import UnknownWriteToolsError, enabled_write_tools, mcp

    try:
        enabled_write_tools()
    except UnknownWriteToolsError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc

    if args.http:
        from .http_transport import configure_http

        configure_http(mcp, args.host, args.port)
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
