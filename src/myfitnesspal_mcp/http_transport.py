"""Streamable HTTP bind policy and bearer gate.

stdio is the default transport. HTTP mode refuses to start unless a shared
secret is configured, stays on loopback unless a LAN bind is explicitly
allowed, and checks the Host header so a DNS-rebinding site cannot call the
port with the attacker's hostname.
"""

import os
import secrets
import sys

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

from . import config

MIN_HTTP_TOKEN_LENGTH = 16
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_WILDCARD_BINDS = {"0.0.0.0", "::", "[::]"}


def _fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def is_loopback(host: str) -> bool:
    return host.strip().lower() in _LOOPBACK_HOSTS


def _token() -> str:
    token = os.environ.get("MFP_HTTP_TOKEN", "").strip()
    if len(token) < MIN_HTTP_TOKEN_LENGTH:
        _fail(
            "Refusing to start HTTP mode without MFP_HTTP_TOKEN set to a secret "
            f"of at least {MIN_HTTP_TOKEN_LENGTH} characters. The default "
            "transport is stdio, which does not use this token. Generate one "
            "with: openssl rand -hex 24"
        )
    return token


def _allowed_names(bind_host: str) -> list[str]:
    if is_loopback(bind_host):
        return ["127.0.0.1", "localhost", "[::1]"]

    if not config.truthy("MFP_HTTP_ALLOW_LAN"):
        _fail(
            f"Refusing to bind HTTP to {bind_host}. The default is 127.0.0.1. "
            "On a trusted LAN, set MFP_HTTP_ALLOW_LAN=1 and "
            "MFP_HTTP_ALLOWED_HOSTS to the hostnames clients will send, and "
            "keep MFP_HTTP_TOKEN set. Do not publish this port to the internet."
        )

    names: list[str] = []
    raw = os.environ.get("MFP_HTTP_ALLOWED_HOSTS", "")
    for part in raw.split(","):
        name = part.strip()
        if name:
            names.append(name)
    if bind_host not in _WILDCARD_BINDS and bind_host not in names:
        names.insert(0, bind_host)
    if not names:
        _fail(
            "Refusing to bind HTTP on all interfaces without "
            "MFP_HTTP_ALLOWED_HOSTS. Set it to the hostname or IP clients use "
            "(for example 192.168.1.20) so the Host check can reject a "
            "DNS-rebinding site."
        )
    for name in names:
        if any(character in name for character in " /\\"):
            _fail(f"Refusing invalid MFP_HTTP_ALLOWED_HOSTS entry {name!r}.")
    return names


def _resource_url(port: int, names: list[str]) -> str:
    return f"http://{names[0]}:{port}/mcp"


class StaticBearerVerifier:
    """Compares the Authorization bearer to MFP_HTTP_TOKEN."""

    def __init__(self, expected: str):
        self._expected = expected.encode("utf-8")

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            presented = token.encode("utf-8")
        except UnicodeError:
            return None
        if not secrets.compare_digest(presented, self._expected):
            return None
        return AccessToken(token=token, client_id="mfp-http", scopes=["mfp"])


def configure_http(mcp, host: str, port: int) -> None:
    """Apply bind, Host/Origin checks, and bearer auth to an existing FastMCP.

    Raises SystemExit before serving when the bind or token is not acceptable.
    Does not print the token.
    """
    token = _token()
    names = _allowed_names(host.strip())
    resource = _resource_url(port, names)
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{name}:*" for name in names],
        allowed_origins=[
            origin
            for name in names
            for origin in (f"http://{name}:*", f"https://{name}:*")
        ],
    )
    mcp.settings.auth = AuthSettings(
        issuer_url=AnyHttpUrl(resource),
        resource_server_url=AnyHttpUrl(resource),
        required_scopes=["mfp"],
        validate_token_resource=False,
    )
    mcp._token_verifier = StaticBearerVerifier(token)
