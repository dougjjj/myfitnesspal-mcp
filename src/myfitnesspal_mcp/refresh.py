"""Headless session refresh and proactive keepalive.

The logged-in website (web-main v21.14.10) sets NextAuth
`sessionProvider.refetchInterval` from `SESSION_REFETCH_INTERVAL=120` and
multiplies it by 1000, so an open tab calls `GET /api/auth/session` every
120 seconds and again when the tab becomes visible. That request is what
rolls `__Secure-next-auth.session-token`. The bundled NextAuth JWT helper
defaults `maxAge` to 30 days; `updateAge` is not in the client bundle, and
the JWT strategy re-issues the session cookie when that endpoint is fetched.

This process does not keep a tab open. It exchanges the saved session cookie
for an API token with `GET /user/auth_token?refresh=true` (access_token,
refresh_token, expires_in, user_id). On a real account that cookie stops
being accepted about 1–2 hours after it is captured, which is far inside the
30-day JWT default. The public assets do not publish the server `maxAge` or
the inner token TTL. Once the exchange returns the logged-out HTML page,
python-myfitnesspal raises "Could not access MyFitnessPal using the cookies
provided by your browser". The persistent profile holds the same cookie, so
opening it after expiry cannot log in again.

`__Host-next-auth.csrf-token` and `__Secure-next-auth.callback-url` are
sign-in bookkeeping. The session poll and the auth-token exchange
authenticate with the session cookie. The web app deletes the legacy
`known_user` cookie on logout; the exchange does not read it.

Keepalive therefore loads the seeded profile, requests those two URLs while
the cookie is still valid, and writes the profile's cookies back to
cookies.json. A homepage visit plus a few seconds does not wait for the
120-second poll, so it does not roll the cookie.
"""

import json
import re
import signal
import sys
import threading
import time
from pathlib import Path
from urllib import parse

from . import auth, config, mfp_client

ORIGIN = "https://www.myfitnesspal.com/"
# The poll the website schedules every 120 seconds.
SESSION_POLL_URL = parse.urljoin(ORIGIN, "api/auth/session")
# The exchange this server uses to build a client. refresh=true asks MFP
# to rotate the API token while the session cookie is still accepted.
AUTH_TOKEN_URL = parse.urljoin(ORIGIN, "user/auth_token") + "?refresh=true"
ROTATION_URLS = (SESSION_POLL_URL, AUTH_TOKEN_URL)
DEFAULT_LOOP_SECONDS = 20 * 60
_MIN_LOOP_SECONDS = 60

_INTERVAL = re.compile(
    r"^\s*(\d+)\s*(s|sec|secs|seconds|m|min|mins|minutes|h|hr|hrs|hours)?\s*$",
    re.IGNORECASE,
)
_INTERVAL_FACTORS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hours": 3600,
}


def enabled() -> bool:
    """Headless refresh stays off unless the operator opts in.

    Installing the [autorefresh] extra is not enough: a persistent Chromium
    profile holds a second copy of the session cookie.
    """
    return config.truthy("MFP_AUTOREFRESH")


def playwright_installed() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def available() -> bool:
    return enabled() and playwright_installed()


def profile_dir() -> Path:
    path = config.data_dir() / "browser-profile"
    if path.exists():
        try:
            path.chmod(0o700)
        except OSError:
            pass
    return path


def profile_seeded() -> bool:
    return profile_dir().is_dir()


def parse_interval(text: str) -> int:
    """Seconds for `keepalive --interval`. Bare numbers are minutes."""
    match = _INTERVAL.match(text or "")
    if not match:
        raise ValueError("interval must look like 20m, 1h, or 90s")
    amount = int(match.group(1))
    unit = (match.group(2) or "m").lower()
    seconds = amount * _INTERVAL_FACTORS[unit]
    if seconds < _MIN_LOOP_SECONDS:
        raise ValueError("interval must be at least 1 minute")
    return seconds


def has_session_cookie(cookies: dict[str, str]) -> bool:
    name = auth.SESSION_COOKIE
    return any(key == name or key.startswith(name + ".") for key in cookies)


def _json_object(body: str) -> dict | None:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and payload:
        return payload
    return None


def session_response_is_live(status: int, content_type: str, body: str) -> bool:
    """NextAuth returns `null` or `{}` when the session cookie is logged out."""
    if status != 200 or "json" not in (content_type or "").lower():
        return False
    return _json_object(body) is not None


def auth_token_response_is_live(status: int, content_type: str, body: str) -> bool:
    """The Python client treats a non-JSON body as a logged-out page."""
    if status != 200 or not (content_type or "").lower().startswith("application/json"):
        return False
    return _json_object(body) is not None


def responses_are_live(responses: list[dict]) -> bool:
    if len(responses) != len(ROTATION_URLS):
        return False
    session, token = responses
    return session_response_is_live(
        session.get("status", 0),
        session.get("content_type", ""),
        session.get("body") or "",
    ) and auth_token_response_is_live(
        token.get("status", 0),
        token.get("content_type", ""),
        token.get("body") or "",
    )


def _cookie_payloads(cookies: dict[str, str]) -> list[dict]:
    payloads = []
    for name, value in cookies.items():
        if not name or value is None:
            continue
        item = {"name": name, "value": value, "path": "/", "secure": True}
        # __Host- cookies are rejected when a Domain attribute is set.
        if name.startswith("__Host-"):
            item["url"] = ORIGIN
        else:
            item["domain"] = ".myfitnesspal.com"
        payloads.append(item)
    return payloads


def _read_response(response) -> dict:
    if response is None:
        return {"status": 0, "content_type": "", "body": ""}
    headers = response.headers or {}
    try:
        body = response.text()
    except Exception:
        # The body can contain an access token. Drop it; an unreadable
        # body fails the live check below.
        body = ""
    return {
        "status": response.status,
        "content_type": headers.get("content-type", ""),
        "body": body,
    }


def _rotate_session(
    seed_cookies: dict[str, str] | None,
) -> tuple[dict[str, str], list[dict]]:
    """Open the persistent profile and request the two rotation URLs.

    Response bodies are for the liveness check only. Callers must not log them.
    """
    from playwright.sync_api import sync_playwright

    responses: list[dict] = []
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile_dir()), headless=True
        )
        try:
            profile_dir()
            if seed_cookies:
                context.add_cookies(_cookie_payloads(seed_cookies))
            page = context.pages[0] if context.pages else context.new_page()
            for url in ROTATION_URLS:
                response = page.goto(url, wait_until="load", timeout=30000)
                responses.append(_read_response(response))
            harvested = {
                cookie["name"]: cookie["value"]
                for cookie in context.cookies(ORIGIN)
                if cookie.get("name") and cookie.get("value") is not None
            }
            return harvested, responses
        finally:
            context.close()


def _visit_and_harvest(seed_cookies: dict[str, str] | None) -> dict[str, str]:
    harvested, _responses = _rotate_session(seed_cookies)
    return harvested


def seed_profile(cookies: dict[str, str]) -> None:
    harvested = _visit_and_harvest(cookies)
    if not has_session_cookie(harvested):
        raise RuntimeError(
            "the browser visit did not produce a MyFitnessPal session cookie"
        )


_refresh_lock = threading.Lock()
_completed_refreshes = 0


def refresh_session() -> None:
    """Reload a newer cookies.json, or roll a still-valid profile cookie.

    A cookies.json written by keepalive wins over the headless browser. The
    profile cannot mint a login once its copy of the cookie has expired, and
    its harvest must not replace a file that changed while the browser ran.
    """
    global _completed_refreshes
    refreshes_seen = _completed_refreshes
    with _refresh_lock:
        if _completed_refreshes != refreshes_seen:
            return
        try:
            if mfp_client.loaded_client_cookie_file_changed():
                return
            mtime_before = mfp_client.cookie_mtime_ns()
            if available() and profile_seeded():
                harvested = _visit_and_harvest(None)
                file_unchanged = mfp_client.cookie_mtime_ns() == mtime_before
                if harvested and has_session_cookie(harvested) and file_unchanged:
                    auth.save_cookies(harvested)
        finally:
            mfp_client.reset()
            _completed_refreshes += 1


_stop = False


def _request_stop(signum, _frame) -> None:
    global _stop
    _stop = True


def _sleep(seconds: float) -> bool:
    """Return True when a signal asked the loop to exit."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _stop:
            return True
        time.sleep(min(1.0, deadline - time.monotonic()))
    return _stop


def keepalive_once() -> tuple[bool, str]:
    """Roll the session once. The message is safe to print; it has no cookies."""
    if not playwright_installed():
        return (
            False,
            "Keepalive needs the [autorefresh] extra. Install it and run "
            "`playwright install chromium`, then retry.",
        )
    if not profile_seeded():
        return (
            False,
            "No browser profile at "
            f"{profile_dir()}. Run `MFP_AUTOREFRESH=1 mfp-mcp auth` once "
            "so keepalive has a profile to refresh.",
        )
    seed = auth.load_cookies()
    try:
        harvested, responses = _rotate_session(seed)
    except Exception as exc:
        return False, f"Keepalive could not reach MyFitnessPal ({exc})."
    if not responses_are_live(responses) or not has_session_cookie(harvested):
        return (
            False,
            "MyFitnessPal session is already dead, so keepalive cannot refresh "
            "it. The browser profile's copy expires with the cookie. Run "
            "'mfp-mcp auth' with a fresh __Secure-next-auth.session-token.",
        )
    auth.save_cookies(harvested)
    mfp_client.reset()
    message = f"Session refreshed and written to {config.cookies_path()} (mode 0600)."
    if config.cookie_env():
        message += (
            " MFP_COOKIE is set, so a running server keeps using that value. "
            "Unset MFP_COOKIE to pick up this file."
        )
    return True, message


def _report(result: tuple[bool, str]) -> int:
    ok, message = result
    print(message, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


def run_keepalive(*, loop: bool = False, interval: int | None = None) -> int:
    global _stop
    _stop = False
    if not loop:
        return _report(keepalive_once())

    seconds = DEFAULT_LOOP_SECONDS if interval is None else interval
    previous_int = signal.signal(signal.SIGINT, _request_stop)
    previous_term = signal.signal(signal.SIGTERM, _request_stop)
    try:
        while not _stop:
            code = _report(keepalive_once())
            if code != 0:
                return code
            if _sleep(seconds):
                return 0
        return 0
    finally:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
