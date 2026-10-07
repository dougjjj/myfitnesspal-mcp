import pytest
from starlette.testclient import TestClient

from myfitnesspal_mcp import cli, http_transport, server

TOKEN = "s" * 24


@pytest.fixture
def restore_mcp():
    settings = server.mcp.settings
    saved = (
        settings.host,
        settings.port,
        settings.auth,
        settings.transport_security,
        server.mcp._token_verifier,
        server.mcp._session_manager,
    )
    yield
    (
        settings.host,
        settings.port,
        settings.auth,
        settings.transport_security,
        server.mcp._token_verifier,
        server.mcp._session_manager,
    ) = saved


def test_parser_defaults_to_stdio_on_loopback():
    args = cli.build_parser().parse_args([])
    assert args.command == "serve"
    assert args.http is False
    assert args.host == "127.0.0.1"
    assert args.port == 8484


def test_http_refuses_to_start_without_a_token(monkeypatch, restore_mcp):
    monkeypatch.delenv("MFP_HTTP_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        http_transport.configure_http(server.mcp, "127.0.0.1", 8484)
    assert exit_info.value.code == 2


def test_http_refuses_a_short_token(monkeypatch, restore_mcp):
    monkeypatch.setenv("MFP_HTTP_TOKEN", "short-token")
    with pytest.raises(SystemExit):
        http_transport.configure_http(server.mcp, "127.0.0.1", 8484)


def test_http_refuses_non_loopback_without_lan_opt_in(monkeypatch, restore_mcp, capsys):
    monkeypatch.setenv("MFP_HTTP_TOKEN", TOKEN)
    monkeypatch.delenv("MFP_HTTP_ALLOW_LAN", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        http_transport.configure_http(server.mcp, "0.0.0.0", 8484)
    assert exit_info.value.code == 2
    assert "127.0.0.1" in capsys.readouterr().err


def test_wildcard_bind_requires_allowed_hosts(monkeypatch, restore_mcp, capsys):
    monkeypatch.setenv("MFP_HTTP_TOKEN", TOKEN)
    monkeypatch.setenv("MFP_HTTP_ALLOW_LAN", "1")
    monkeypatch.delenv("MFP_HTTP_ALLOWED_HOSTS", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        http_transport.configure_http(server.mcp, "0.0.0.0", 8484)
    assert exit_info.value.code == 2
    assert "MFP_HTTP_ALLOWED_HOSTS" in capsys.readouterr().err


def test_http_requires_bearer_and_rejects_a_rebound_host(monkeypatch, restore_mcp):
    monkeypatch.setenv("MFP_HTTP_TOKEN", TOKEN)
    server.mcp._session_manager = None
    http_transport.configure_http(server.mcp, "127.0.0.1", 8484)
    app = server.mcp.streamable_http_app()

    with TestClient(app, base_url="http://127.0.0.1:8484") as client:
        missing = client.post(
            "/mcp",
            headers={"content-type": "application/json"},
            content=b"{}",
        )
        wrong = client.post(
            "/mcp",
            headers={
                "content-type": "application/json",
                "authorization": "Bearer not-the-token",
            },
            content=b"{}",
        )
        accepted = client.post(
            "/mcp",
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {TOKEN}",
            },
            content=b"{}",
        )
        rebound = client.post(
            "/mcp",
            headers={
                "host": "evil.example:8484",
                "content-type": "application/json",
                "authorization": f"Bearer {TOKEN}",
            },
            content=b"{}",
        )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert accepted.status_code != 401
    assert rebound.status_code == 421
    assert TOKEN not in missing.text
    assert TOKEN not in wrong.text
