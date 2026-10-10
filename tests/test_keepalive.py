import json
import os
import sys

import pytest

from myfitnesspal_mcp import auth, cli, mfp_client, refresh

LIVE_SESSION = {
    "status": 200,
    "content_type": "application/json",
    "body": '{"user":{"name":"tester"},"expires":"2099-01-01T00:00:00.000Z"}',
}
LIVE_TOKEN = {
    "status": 200,
    "content_type": "application/json; charset=utf-8",
    "body": '{"user_id":"user-1","access_token":"fake-access","expires_in":3600}',
}


@pytest.fixture
def cookie_file(tmp_path, monkeypatch):
    path = tmp_path / "cookies.json"
    monkeypatch.setattr(auth.config, "cookies_path", lambda: path)
    monkeypatch.setattr(refresh.config, "cookies_path", lambda: path)
    monkeypatch.delenv("MFP_COOKIE", raising=False)
    monkeypatch.setattr(refresh, "playwright_installed", lambda: True)
    monkeypatch.setattr(refresh, "profile_seeded", lambda: True)
    monkeypatch.setattr(refresh, "profile_dir", lambda: tmp_path / "browser-profile")
    return path


def test_parse_interval_units():
    assert refresh.parse_interval("20m") == 1200
    assert refresh.parse_interval("20") == 1200
    assert refresh.parse_interval("1h") == 3600
    assert refresh.parse_interval("90s") == 90
    with pytest.raises(ValueError, match="at least 1 minute"):
        refresh.parse_interval("30s")
    with pytest.raises(ValueError, match="20m"):
        refresh.parse_interval("soon")


def test_live_responses_require_both_documents():
    assert refresh.responses_are_live([LIVE_SESSION, LIVE_TOKEN])
    logged_out = [
        {"status": 200, "content_type": "application/json", "body": "null"},
        {"status": 200, "content_type": "text/html", "body": "<html></html>"},
    ]
    assert refresh.responses_are_live(logged_out) is False
    assert (
        refresh.responses_are_live(
            [
                LIVE_SESSION,
                {"status": 200, "content_type": "text/html", "body": "<html>"},
            ]
        )
        is False
    )
    assert refresh.session_response_is_live(200, "application/json", "{}") is False


def test_chunked_session_cookie_and_host_prefix():
    assert refresh.has_session_cookie({auth.SESSION_COOKIE + ".0": "part"})
    assert refresh.has_session_cookie({"known_user": "1"}) is False
    payloads = refresh._cookie_payloads(
        {
            auth.SESSION_COOKIE: "session-value",
            "__Host-next-auth.csrf-token": "csrf-value",
        }
    )
    by_name = {item["name"]: item for item in payloads}
    assert by_name[auth.SESSION_COOKIE]["domain"] == ".myfitnesspal.com"
    host_cookie = by_name["__Host-next-auth.csrf-token"]
    assert "domain" not in host_cookie
    assert host_cookie["url"] == refresh.ORIGIN


def test_rotation_urls_match_the_website_poll_and_the_client_exchange():
    assert refresh.SESSION_POLL_URL.endswith("/api/auth/session")
    assert refresh.AUTH_TOKEN_URL.endswith("/user/auth_token?refresh=true")
    assert refresh.ROTATION_URLS == (refresh.SESSION_POLL_URL, refresh.AUTH_TOKEN_URL)


def test_keepalive_writes_rotated_cookie_privately(cookie_file, monkeypatch, capsys):
    auth.save_cookies({auth.SESSION_COOKIE: "session-old"}, username="tester")
    seen = {}

    def rotate(seed):
        seen["seed"] = seed
        return {auth.SESSION_COOKIE: "rotated-session"}, [LIVE_SESSION, LIVE_TOKEN]

    monkeypatch.setattr(refresh, "_rotate_session", rotate)
    assert refresh.run_keepalive() == 0

    saved = json.loads(cookie_file.read_text())
    assert saved["cookies"] == {auth.SESSION_COOKIE: "rotated-session"}
    assert saved["username"] == "tester"
    assert cookie_file.stat().st_mode & 0o777 == 0o600
    assert seen["seed"][auth.SESSION_COOKIE] == "session-old"
    captured = capsys.readouterr()
    assert "0600" in captured.out
    assert "rotated-session" not in captured.out
    assert "rotated-session" not in captured.err
    assert "fake-access" not in captured.out
    assert "fake-access" not in captured.err


def test_keepalive_dead_session_keeps_the_saved_cookie(
    cookie_file, monkeypatch, capsys
):
    auth.save_cookies({auth.SESSION_COOKIE: "session-old"}, username="tester")
    before = cookie_file.read_text()

    def rotate(seed):
        return {auth.SESSION_COOKIE: "still-there"}, [
            {"status": 200, "content_type": "application/json", "body": "null"},
            {"status": 200, "content_type": "text/html", "body": "<html></html>"},
        ]

    monkeypatch.setattr(refresh, "_rotate_session", rotate)
    assert refresh.run_keepalive() == 1

    assert cookie_file.read_text() == before
    captured = capsys.readouterr()
    assert "already dead" in captured.err
    assert "mfp-mcp auth" in captured.err
    assert "still-there" not in captured.err
    assert "still-there" not in captured.out


def test_keepalive_missing_profile_does_not_open_a_browser(
    cookie_file, monkeypatch, capsys
):
    monkeypatch.setattr(refresh, "profile_seeded", lambda: False)
    monkeypatch.setattr(
        refresh,
        "_rotate_session",
        lambda seed: (_ for _ in ()).throw(AssertionError("browser launched")),
    )
    assert refresh.run_keepalive() == 1
    assert "No browser profile" in capsys.readouterr().err
    assert not cookie_file.exists()


def test_keepalive_loop_stops_when_the_session_dies(cookie_file, monkeypatch):
    calls = {"n": 0}
    slept = []

    def once():
        calls["n"] += 1
        if calls["n"] == 1:
            return True, "refreshed"
        return False, "MyFitnessPal session is already dead"

    monkeypatch.setattr(refresh, "keepalive_once", once)
    monkeypatch.setattr(
        refresh, "_sleep", lambda seconds: slept.append(seconds) or False
    )

    assert refresh.run_keepalive(loop=True, interval=1200) == 1
    assert calls["n"] == 2
    assert slept == [1200]


def test_keepalive_loop_exits_cleanly_when_stopped(cookie_file, monkeypatch):
    monkeypatch.setattr(refresh, "keepalive_once", lambda: (True, "refreshed"))
    monkeypatch.setattr(refresh, "_sleep", lambda seconds: True)
    assert refresh.run_keepalive(loop=True) == 0


def test_keepalive_command_parses_loop_interval(monkeypatch):
    seen = {}

    def run(*, loop, interval):
        seen["loop"] = loop
        seen["interval"] = interval
        return 0

    monkeypatch.setattr(
        sys, "argv", ["mfp-mcp", "keepalive", "--loop", "--interval", "20m"]
    )
    monkeypatch.setattr(refresh, "run_keepalive", run)
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    assert seen == {"loop": True, "interval": 1200}


def test_keepalive_interval_without_loop_is_rejected(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["mfp-mcp", "keepalive", "--interval", "20m"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 2


def test_client_reloads_when_cookie_file_mtime_changes(cookie_file, monkeypatch):
    built = []

    class Sentinel:
        def __init__(self, cookies):
            self.cookies = cookies

    def build(cookies, username=None, impersonate=None):
        client = Sentinel(dict(cookies))
        built.append(client)
        return client

    monkeypatch.setattr(mfp_client, "build_client", build)
    monkeypatch.setattr(auth.config, "cookies_path", lambda: cookie_file)
    monkeypatch.setattr(mfp_client.config, "cookies_path", lambda: cookie_file)
    mfp_client.reset()
    try:
        auth.save_cookies({auth.SESSION_COOKIE: "session-old"})
        first = mfp_client.get_client()
        assert first.cookies[auth.SESSION_COOKIE] == "session-old"
        auth.save_cookies({auth.SESSION_COOKIE: "session-new"})
        # A rewrite in the same clock tick can keep the previous mtime.
        later = cookie_file.stat().st_mtime + 2
        os.utime(cookie_file, (later, later))
        second = mfp_client.get_client()
        assert second is not first
        assert second.cookies[auth.SESSION_COOKIE] == "session-new"
        assert mfp_client.get_client() is second
        assert len(built) == 2
    finally:
        mfp_client.reset()


def test_env_cookie_ignores_a_rewritten_file(cookie_file, monkeypatch):
    def build(cookies, username=None, impersonate=None):
        return {"cookies": dict(cookies)}

    monkeypatch.setattr(mfp_client, "build_client", build)
    monkeypatch.setattr(mfp_client.config, "cookies_path", lambda: cookie_file)
    mfp_client.reset()
    try:
        auth.save_cookies({auth.SESSION_COOKIE: "from-file"})
        monkeypatch.setenv("MFP_COOKIE", "from-env")
        first = mfp_client.get_client()
        assert first["cookies"] == {auth.SESSION_COOKIE: "from-env"}
        auth.save_cookies({auth.SESSION_COOKIE: "rewritten"})
        later = cookie_file.stat().st_mtime + 2
        os.utime(cookie_file, (later, later))
        assert mfp_client.get_client() is first
    finally:
        mfp_client.reset()
