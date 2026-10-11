import getpass
import json
import os
import sys

from . import config

SESSION_COOKIE = "__Secure-next-auth.session-token"

INSTRUCTIONS = """\
Connect your MyFitnessPal account
---------------------------------
1. Log in at https://www.myfitnesspal.com in your browser.
2. Open DevTools (F12) -> Application (Chrome) or Storage (Firefox) -> Cookies.
3. Copy ONLY the value of '__Secure-next-auth.session-token'.
   That cookie is full access to the account. Do not paste a full Cookie
   header, and do not put the value in an MCP client JSON file.
"""


def parse_cookie_input(text: str) -> dict[str, str]:
    text = text.strip()
    if text.lower().startswith("cookie:"):
        text = text[len("cookie:") :].strip()
    if "=" not in text:
        return {SESSION_COOKIE: text}
    cookies = {}
    for part in text.split(";"):
        if "=" in part:
            name, value = part.strip().split("=", 1)
            cookies[name.strip()] = value.strip()
    return cookies


def _read_saved() -> dict:
    path = config.cookies_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def stored_cookies() -> dict[str, str]:
    """The full jar in cookies.json.

    `MFP_COOKIE` is one token and hides this file from `load_cookies`.
    Keepalive has to seed the browser with every saved cookie, because the
    API exchange can still succeed after the NextAuth token alone has expired.
    """
    saved = _read_saved().get("cookies")
    if not isinstance(saved, dict):
        return {}
    return {str(name): str(value) for name, value in saved.items() if value is not None}


def load_cookies() -> dict[str, str] | None:
    env = config.cookie_env()
    if env:
        return parse_cookie_input(env)
    saved = stored_cookies()
    if saved:
        return saved
    return None


def saved_username() -> str | None:
    if config.username_env():
        return config.username_env()
    return _read_saved().get("username")


def save_cookies(cookies: dict[str, str], username: str | None = None) -> None:
    saved = _read_saved()
    saved["cookies"] = cookies
    if username:
        saved["username"] = username
    path = config.cookies_path()
    payload = json.dumps(saved, indent=2)
    # O_CREAT with 0600 so the file is never briefly world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(payload)
    path.chmod(0o600)


def read_cookie_paste() -> str:
    if sys.stdin.isatty():
        return getpass.getpass("Paste cookie (input hidden): ")
    print("(no terminal detected — reading the cookie from stdin)", file=sys.stderr)
    return sys.stdin.readline()


def _validate(cookies: dict[str, str]):
    """Builds a client from the cookies; when only the profile lookup fails (a
    known MFP issue for accounts that log in by email), asks for the username
    and retries instead of failing the whole flow."""
    from myfitnesspal.exceptions import MyfitnesspalLoginError

    from . import mfp_client

    try:
        return mfp_client.build_client(cookies)
    except MyfitnesspalLoginError as exc:
        if "profile" not in str(exc):
            raise
        if not sys.stdin.isatty():
            raise
        print(
            "\nYour cookie works, but MyFitnessPal couldn't return your profile "
            "(a known issue for some accounts)."
        )
        username = input("Your MyFitnessPal username (not email): ").strip()
        if not username:
            raise
        save_cookies(cookies, username=username)
        return mfp_client.build_client(cookies)


def session_source() -> str | None:
    if config.cookie_env():
        return "the MFP_COOKIE environment variable"
    if _read_saved().get("cookies"):
        return str(config.cookies_path())
    return None


def auto_refresh_status() -> tuple[bool, str]:
    from . import refresh

    if not refresh.enabled():
        return (
            False,
            "off (set MFP_AUTOREFRESH=1 to allow the headless-browser extra)",
        )
    if not refresh.playwright_installed():
        return False, "requested, but the [autorefresh] extra is not installed"
    if not refresh.profile_seeded():
        return False, "enabled, but no browser profile yet (run auth again)"
    return True, f"ready (profile at {refresh.profile_dir()})"


def run_check() -> int:
    from . import mfp_client

    source = session_source()
    auto_refresh_ready, auto_refresh_summary = auto_refresh_status()
    if source is None:
        print("Session:      none saved")
        print(f"Auto-refresh: {auto_refresh_summary}")
        print("Fix: run 'myfitnesspal-mcp auth' to connect your account.")
        return 1

    print(f"Session:      from {source}")
    try:
        client = mfp_client.build_client(load_cookies())
    except Exception as exc:
        print(f"Status:       rejected ({exc})")
        print(f"Auto-refresh: {auto_refresh_summary}")
        if auto_refresh_ready:
            print(
                "Fix: the server will try a headless-browser refresh on the next "
                "tool call; if that fails, run 'myfitnesspal-mcp auth'."
            )
        else:
            print("Fix: run 'myfitnesspal-mcp auth' with a fresh cookie.")
        return 1

    print(f"Status:       valid, connected as {client.effective_username}")
    print(f"Auto-refresh: {auto_refresh_summary}")
    return 0


def run_auth_flow() -> int:
    from . import mfp_client, refresh

    env_cookie = config.cookie_env()
    if env_cookie:
        print(
            "Using MFP_COOKIE for this auth command only. It will be written to "
            f"{config.cookies_path()} with mode 0600. Unset MFP_COOKIE before "
            "starting the server so the process reads that file.",
            file=sys.stderr,
        )
        pasted = env_cookie
    else:
        print(INSTRUCTIONS, flush=True)
        try:
            pasted = read_cookie_paste()
        except EOFError:
            pasted = ""
    if not pasted.strip():
        print(
            "No cookie received. On a headless machine, either pipe the token "
            "(printf '%s\\n' \"$COOKIE\" | mfp-mcp auth) or run once with "
            "MFP_COOKIE set. Both write cookies.json with mode 0600. Then unset "
            "MFP_COOKIE. Do not commit token.txt or put the cookie in MCP JSON.",
            file=sys.stderr,
        )
        return 1

    cookies = parse_cookie_input(pasted)
    print("Validating with MyFitnessPal...")
    try:
        client = _validate(cookies)
    except Exception as exc:
        print(f"Those cookies didn't authenticate: {exc}", file=sys.stderr)
        print(
            "If this mentions Cloudflare or a 403, try MFP_IMPERSONATE=chrome124 "
            "or run from a residential IP.",
            file=sys.stderr,
        )
        return 1

    save_cookies(cookies, username=client.effective_username)
    mfp_client.reset()
    print(
        f"Connected as {client.effective_username}. Cookies saved to {config.cookies_path()}"
    )

    if refresh.available():
        print("Seeding the browser profile for automatic session refresh...")
        try:
            refresh.seed_profile(cookies)
            print(f"Auto-refresh ready (profile at {refresh.profile_dir()}).")
        except Exception as exc:
            print(
                f"Could not seed the auto-refresh browser profile: {exc}",
                file=sys.stderr,
            )
            print(
                "The server still works; sessions just need a manual re-auth when they expire."
            )
    elif refresh.enabled():
        print(
            "MFP_AUTOREFRESH is set, but the [autorefresh] extra is not installed. "
            "Sessions need a manual re-auth when they expire."
        )
    else:
        print(
            "Auto-refresh is off. Set MFP_AUTOREFRESH=1 and install the "
            "[autorefresh] extra only if you want a headless browser to renew "
            "the session. Otherwise re-run auth when it expires."
        )
    return 0
