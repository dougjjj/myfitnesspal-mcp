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
    status = tmp_path / "keepalive.status"
    monkeypatch.setattr(refresh, "keepalive_status_path", lambda: status)
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
    session = by_name[auth.SESSION_COOKIE]
    assert session["domain"] == ".myfitnesspal.com"
    assert session["path"] == "/"
    assert "url" not in session
    host_cookie = by_name["__Host-next-auth.csrf-token"]
    assert "domain" not in host_cookie
    assert "path" not in host_cookie
    assert host_cookie["url"] == refresh.ORIGIN


def test_second_keepalive_reseeds_harvested_host_cookie(cookie_file, monkeypatch):
    """The first harvest includes the csrf cookie; the next run must reseed it."""
    auth.save_cookies({auth.SESSION_COOKIE: "session-old"}, username="tester")
    harvested = {
        auth.SESSION_COOKIE: "rotated-session",
        "__Host-next-auth.csrf-token": "csrf-value",
        "__Secure-next-auth.callback-url": "https://www.myfitnesspal.com/",
    }
    seeds = []

    def rotate(seed):
        seeds.append(dict(seed or {}))
        return dict(harvested), [LIVE_SESSION, LIVE_TOKEN]

    monkeypatch.setattr(refresh, "_rotate_session", rotate)
    assert refresh.run_keepalive() == 0
    assert refresh.run_keepalive() == 0

    assert "__Host-next-auth.csrf-token" not in seeds[0]
    assert seeds[1]["__Host-next-auth.csrf-token"] == "csrf-value"
    assert seeds[1][auth.SESSION_COOKIE] == "rotated-session"
    payloads = refresh._cookie_payloads(seeds[1])
    assert not any("url" in item and "path" in item for item in payloads)
    host_cookie = next(
        item for item in payloads if item["name"] == "__Host-next-auth.csrf-token"
    )
    assert host_cookie["url"] == refresh.ORIGIN
    assert "path" not in host_cookie
    session = next(item for item in payloads if item["name"] == auth.SESSION_COOKIE)
    assert session["path"] == "/"
    assert "url" not in session
    saved = json.loads(cookie_file.read_text())
    assert saved["cookies"]["__Host-next-auth.csrf-token"] == "csrf-value"
    assert saved["username"] == "tester"


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
    cookie_file, tmp_path, monkeypatch, capsys
):
    auth.save_cookies({auth.SESSION_COOKIE: "session-old"}, username="tester")
    before = cookie_file.read_text()
    calls = []

    def rotate(seed, *, remint=False):
        calls.append(remint)
        return {auth.SESSION_COOKIE: "still-there"}, [
            {"status": 200, "content_type": "application/json", "body": "null"},
            {"status": 200, "content_type": "text/html", "body": "<html></html>"},
        ]

    monkeypatch.setattr(refresh, "_rotate_session", rotate)
    monkeypatch.setattr(refresh, "api_cookies_still_valid", lambda cookies: False)
    assert refresh.run_keepalive() == 1

    assert calls == [False]
    assert cookie_file.read_text() == before
    captured = capsys.readouterr()
    assert "logged out" in captured.err
    assert "left unchanged" in captured.err
    assert "mfp-mcp auth" in captured.err
    assert "still-there" not in captured.err
    assert "still-there" not in captured.out
    status_path = tmp_path / "keepalive.status"
    status = json.loads(status_path.read_text())
    assert status["consecutive_failures"] == 1
    assert status["last_success"] is None
    assert "session-old" not in status["last_error"]
    assert "still-there" not in status["last_error"]
    assert status_path.stat().st_mode & 0o777 == 0o600


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


def test_keepalive_loop_retries_until_stopped(
    cookie_file, tmp_path, monkeypatch, capsys
):
    calls = {"n": 0}
    slept = []

    def once():
        calls["n"] += 1
        if calls["n"] < 5:
            return False, "could not confirm"
        return True, "refreshed"

    def sleep(seconds):
        slept.append(seconds)
        return calls["n"] >= 5

    monkeypatch.setattr(refresh, "keepalive_once", once)
    monkeypatch.setattr(refresh, "_sleep", sleep)

    assert refresh.run_keepalive(loop=True, interval=1200, max_failures=2) == 0
    assert calls["n"] == 5
    assert slept == [120, 300, 600, 1200, 1200]
    captured = capsys.readouterr()
    assert "keepalive alert: 2 consecutive failures" in captured.err
    assert "keepalive alert: 4 consecutive failures" in captured.err
    assert "keepalive alert: 5" not in captured.err
    status = json.loads((tmp_path / "keepalive.status").read_text())
    assert status["consecutive_failures"] == 0
    assert status["last_success"].endswith("Z")
    assert status["last_error"] is None
    assert "alert" not in status


def test_keepalive_loop_exits_cleanly_when_stopped(cookie_file, monkeypatch):
    monkeypatch.setattr(refresh, "keepalive_once", lambda: (True, "refreshed"))
    monkeypatch.setattr(refresh, "_sleep", lambda seconds: True)
    assert refresh.run_keepalive(loop=True) == 0


def test_keepalive_command_parses_loop_interval(monkeypatch):
    seen = {}

    def run(*, loop, interval, max_failures):
        seen["loop"] = loop
        seen["interval"] = interval
        seen["max_failures"] = max_failures
        return 0

    monkeypatch.setattr(
        sys, "argv", ["mfp-mcp", "keepalive", "--loop", "--interval", "20m"]
    )
    monkeypatch.setattr(refresh, "run_keepalive", run)
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    assert seen == {"loop": True, "interval": 1200, "max_failures": None}


def test_keepalive_interval_without_loop_is_rejected(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["mfp-mcp", "keepalive", "--interval", "20m"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 2


def test_keepalive_max_failures_requires_loop(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["mfp-mcp", "keepalive", "--max-failures", "3"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 2
    monkeypatch.setattr(
        sys,
        "argv",
        ["mfp-mcp", "keepalive", "--loop", "--max-failures", "0"],
    )
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 2


def test_keepalive_command_parses_max_failures(monkeypatch):
    seen = {}

    def run(*, loop, interval, max_failures):
        seen["max_failures"] = max_failures
        seen["loop"] = loop
        return 0

    monkeypatch.setattr(
        sys, "argv", ["mfp-mcp", "keepalive", "--loop", "--max-failures", "3"]
    )
    monkeypatch.setattr(refresh, "run_keepalive", run)
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    assert seen == {"loop": True, "max_failures": 3}


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


LOGGED_OUT = {"status": 200, "content_type": "application/json", "body": "null"}


def test_session_state_distinguishes_logout_from_transient_errors():
    assert refresh.session_state(LOGGED_OUT) == refresh.LOGGED_OUT
    assert (
        refresh.session_state(
            {"status": 200, "content_type": "application/json", "body": "{}"}
        )
        == refresh.LOGGED_OUT
    )
    assert refresh.session_state(LIVE_SESSION) == refresh.LIVE
    assert (
        refresh.session_state(
            {"status": 503, "content_type": "text/html", "body": "unavailable"}
        )
        == refresh.TRANSIENT
    )
    assert (
        refresh.session_state(
            {
                "status": 403,
                "content_type": "text/html",
                "body": "<html>Just a moment</html>",
            }
        )
        == refresh.TRANSIENT
    )
    assert (
        refresh.session_state(
            {"status": 200, "content_type": "text/html", "body": "<html>login</html>"}
        )
        == refresh.TRANSIENT
    )
    assert refresh.failure_backoff(1, 1200) == 120
    assert refresh.failure_backoff(2, 1200) == 300
    assert refresh.failure_backoff(3, 1200) == 600
    assert refresh.failure_backoff(4, 1200) == 1200
    urls = refresh.navigation_urls(remint=True)
    assert urls[0] == refresh.DIARY_URL
    assert urls[0].endswith("/food/diary")
    assert refresh.navigation_urls(remint=False) == list(refresh.ROTATION_URLS)


def test_emit_flushes(monkeypatch):
    seen = {}

    def fake_print(*args, **kwargs):
        seen["flush"] = kwargs.get("flush")
        seen["file"] = kwargs.get("file")

    monkeypatch.setattr("builtins.print", fake_print)
    refresh._emit(False, "hello")
    assert seen == {"flush": True, "file": sys.stderr}


def test_transient_response_leaves_cookies_unchanged(cookie_file, monkeypatch, capsys):
    auth.save_cookies({auth.SESSION_COOKIE: "session-old"}, username="tester")
    before = cookie_file.read_text()

    def rotate(seed):
        return {auth.SESSION_COOKIE: "still-there"}, [
            {"status": 503, "content_type": "text/html", "body": "unavailable"},
            LIVE_TOKEN,
        ]

    def api_check(cookies):
        raise AssertionError("transient result must not call the API check")

    monkeypatch.setattr(refresh, "_rotate_session", rotate)
    monkeypatch.setattr(refresh, "api_cookies_still_valid", api_check)
    assert refresh.run_keepalive() == 1
    assert cookie_file.read_text() == before
    captured = capsys.readouterr()
    assert "Cloudflare" in captured.err
    assert "left unchanged" in captured.err
    assert "mfp-mcp auth" not in captured.err


def test_browser_error_is_transient_and_omits_the_exception(
    cookie_file, tmp_path, monkeypatch, capsys
):
    auth.save_cookies({auth.SESSION_COOKIE: "session-old"})

    def rotate(seed):
        raise RuntimeError("cookie=session-old")

    monkeypatch.setattr(refresh, "_rotate_session", rotate)
    assert refresh.run_keepalive() == 1
    captured = capsys.readouterr()
    assert "Cloudflare" in captured.err
    assert "session-old" not in captured.err
    assert "session-old" not in captured.out
    status = json.loads((tmp_path / "keepalive.status").read_text())
    assert "session-old" not in status["last_error"]


def test_keepalive_seeds_the_full_jar_even_when_env_is_one_token(
    cookie_file, monkeypatch, capsys
):
    auth.save_cookies(
        {
            auth.SESSION_COOKIE: "session-old",
            "p": "legacy-session",
            "known_user": "1",
            "_mfp_session": "server-session",
            "__Host-next-auth.csrf-token": "csrf-value",
        },
        username="tester",
    )
    monkeypatch.setenv("MFP_COOKIE", "env-token-only")
    seen = {}

    def rotate(seed):
        seen["seed"] = dict(seed)
        return {
            auth.SESSION_COOKIE: "rotated-session",
            "p": "legacy-session",
        }, [LIVE_SESSION, LIVE_TOKEN]

    monkeypatch.setattr(refresh, "_rotate_session", rotate)
    assert refresh.run_keepalive() == 0
    assert seen["seed"][auth.SESSION_COOKIE] == "session-old"
    assert seen["seed"]["p"] == "legacy-session"
    assert seen["seed"]["_mfp_session"] == "server-session"
    assert seen["seed"]["__Host-next-auth.csrf-token"] == "csrf-value"
    assert "env-token-only" not in seen["seed"].values()
    payloads = refresh._cookie_payloads(seen["seed"])
    assert not any("url" in item and "path" in item for item in payloads)
    saved = json.loads(cookie_file.read_text())["cookies"]
    assert saved[auth.SESSION_COOKIE] == "rotated-session"
    assert saved["p"] == "legacy-session"
    assert saved["known_user"] == "1"
    assert saved["_mfp_session"] == "server-session"
    assert saved["__Host-next-auth.csrf-token"] == "csrf-value"
    assert "MFP_COOKIE" in capsys.readouterr().out


def test_logged_out_session_remints_when_the_jar_still_passes(cookie_file, monkeypatch):
    auth.save_cookies(
        {
            auth.SESSION_COOKIE: "session-old",
            "p": "legacy-session",
            "known_user": "1",
        }
    )
    calls = []

    def rotate(seed, *, remint=False):
        calls.append(remint)
        assert seed["p"] == "legacy-session"
        if not remint:
            return (
                {auth.SESSION_COOKIE: "session-old", "p": "legacy-session"},
                [LOGGED_OUT, LIVE_TOKEN],
            )
        return (
            {auth.SESSION_COOKIE: "minted", "p": "legacy-session"},
            [LIVE_SESSION, LIVE_TOKEN],
        )

    monkeypatch.setattr(refresh, "_rotate_session", rotate)
    monkeypatch.setattr(refresh, "api_cookies_still_valid", lambda cookies: True)
    assert refresh.run_keepalive() == 0
    assert calls == [False, True]
    saved = json.loads(cookie_file.read_text())["cookies"]
    assert saved[auth.SESSION_COOKIE] == "minted"
    assert saved["p"] == "legacy-session"
    assert saved["known_user"] == "1"


def test_remint_that_stays_logged_out_leaves_the_jar(cookie_file, monkeypatch, capsys):
    auth.save_cookies({auth.SESSION_COOKIE: "session-old", "p": "legacy-session"})
    before = cookie_file.read_text()
    calls = []

    def rotate(seed, *, remint=False):
        calls.append(remint)
        return {auth.SESSION_COOKIE: "session-old"}, [
            {"status": 200, "content_type": "application/json", "body": "{}"},
            LIVE_TOKEN,
        ]

    monkeypatch.setattr(refresh, "_rotate_session", rotate)
    monkeypatch.setattr(
        refresh,
        "api_cookies_still_valid",
        lambda cookies: cookies["p"] == "legacy-session",
    )
    assert refresh.run_keepalive() == 1
    assert calls == [False, True]
    assert cookie_file.read_text() == before
    captured = capsys.readouterr()
    assert "diary" in captured.err
    assert "left unchanged" in captured.err
    assert "mfp-mcp auth" not in captured.err
    assert "legacy-session" not in captured.err
