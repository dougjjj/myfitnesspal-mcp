import argparse
import logging
import sys

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def http_bind_is_loopback(host: str) -> bool:
    return host.strip().lower() in _LOOPBACK_HOSTS


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="myfitnesspal-mcp",
        description="MCP server for MyFitnessPal (stdio by default).",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["serve", "auth"],
        default="serve",
        help="serve (default) or auth to connect your MyFitnessPal account",
    )
    parser.add_argument("--http", action="store_true", help="serve over streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8484, help="HTTP port (default 8484)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    if args.command == "auth":
        from .auth import run_auth_flow

        raise SystemExit(run_auth_flow())

    from .server import mcp

    if args.http:
        if not http_bind_is_loopback(args.host):
            print(
                f"WARNING: HTTP mode has no authentication and is binding to "
                f"{args.host}:{args.port}. Any client that can reach that "
                f"address can read and write the connected MyFitnessPal "
                f"account. Prefer stdio, or bind 127.0.0.1 and put an "
                f"authenticating proxy in front. See SECURITY_REVIEW.md.",
                file=sys.stderr,
            )
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
