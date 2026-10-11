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
sign-in bookkeeping. `GET /api/auth/session` decides the NextAuth cookie
alone: HTTP 200 with `null` or `{}` means that cookie is logged out.

`GET /user/auth_token?refresh=true` is a different check. Its JSON body
carries an API `access_token` and `refresh_token` for api.myfitnesspal.com.
That `refresh_token` is not a NextAuth session, and nothing in the public
site turns it into `Set-Cookie` for `__Secure-next-auth.session-token`
(minting that cookie needs the server's NextAuth secret). The client bundle's
`/api/auth/refresh-token-data` route is a test double. python-myfitnesspal
reads the JSON and does not store the refresh token.

On a live account the full `cookies.json` jar still passed that API exchange
after the bare NextAuth token failed. The extra credential is some other
cookie in the jar (the legacy Rails session `p` is the long-lived one the
old site used; `known_user` is only a "this browser has logged in" flag the
web app deletes on logout; `_mfp_session`, when present, is set by the
server and is not named in the public page scripts). Keepalive therefore
seeds the browser with the whole file, and if the session poll is logged
out while the jar still passes the API check, it opens the diary so the
site can bridge that jar into a new NextAuth cookie.

A Cloudflare challenge or an HTTP 5xx is not a logged-out session. The
loop logs it and retries. It exits when the process is stopped, not when
a refresh fails.
"""

import json
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import parse

from . import auth, config, mfp_client

ORIGIN = "https://www.myfitnesspal.com/"
# The poll the website schedules every 120 seconds.
SESSION_POLL_URL = parse.urljoin(ORIGIN, "api/auth/session")
# The exchange this server uses to build a client. refresh=true asks MFP
# to rotate the API token while the session cookie is still accepted.
AUTH_TOKEN_URL = parse.urljoin(ORIGIN, "user/auth_token") + "?refresh=true"
# A logged-in diary page is the request that can Set-Cookie a new NextAuth
# session when a legacy cookie still identifies the account.
DIARY_URL = parse.urljoin(ORIGIN, "food/diary")
ROTATION_URLS = (SESSION_POLL_URL, AUTH_TOKEN_URL)
DEFAULT_LOOP_SECONDS = 20 * 60
_MIN_LOOP_SECONDS = 60
# After a failure the loop waits 2, then 5, then 10 minutes, then the
# normal interval. It does not exit.
FAILURE_BACKOFF_SECONDS = (2 * 60, 5 * 60, 10 * 60)
LIVE = "live"
LOGGED_OUT = "logged_out"
TRANSIENT = "transient"
_CF_MARKERS = (
    "just a moment",
    "cf-browser-verification",
    "challenge-platform",
    "attention required",
    "cf-mitigated",
)

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
    return (
        session_state({"status": status, "content_type": content_type, "body": body})
        == LIVE
    )


def _cloudflare_challenge(status: int, content_type: str, body: str) -> bool:
    sample = (body or "")[:4000].lower()
    if any(marker in sample for marker in _CF_MARKERS):
        return True
    return status in (403, 429, 503) and "html" in (content_type or "").lower()


def session_state(response: dict) -> str:
    """`live`, `logged_out`, or `transient`.

    Only HTTP 200 with a JSON `null` or `{}` from `/api/auth/session` is a
    logged-out NextAuth cookie. A Cloudflare challenge or a 5xx is retried.
    """
    status = int(response.get("status") or 0)
    content_type = response.get("content_type") or ""
    body = response.get("body") or ""
    if (
        status == 0
        or status >= 500
        or _cloudflare_challenge(status, content_type, body)
    ):
        return TRANSIENT
    if status != 200 or "json" not in content_type.lower():
        return TRANSIENT
    text = body.strip()
    if text in ("", "null", "{}"):
        return LOGGED_OUT
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return TRANSIENT
    if payload is None or payload == {}:
        return LOGGED_OUT
    if isinstance(payload, dict) and payload:
        return LIVE
    return TRANSIENT


def failure_backoff(consecutive: int, interval: int) -> int:
    """Seconds to wait after `consecutive` failed attempts."""
    if consecutive <= 0:
        return interval
    if consecutive <= len(FAILURE_BACKOFF_SECONDS):
        return FAILURE_BACKOFF_SECONDS[consecutive - 1]
    return interval


def navigation_urls(*, remint: bool) -> list[str]:
    if remint:
        return [DIARY_URL, *ROTATION_URLS]
    return list(ROTATION_URLS)


def merge_cookies(
    existing: dict[str, str], harvested: dict[str, str]
) -> dict[str, str]:
    """Harvested names win. Cookies the browser did not return stay in the file."""
    merged = dict(existing)
    for name, value in harvested.items():
        if name and value is not None:
            merged[str(name)] = str(value)
    return merged


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
        item = {"name": name, "value": value, "secure": True}
        # __Host- cookies are host-only, so they cannot carry a Domain.
        # Playwright rejects a cookie that sets both url and path, which is
        # what broke the second keepalive once cookies.json held the csrf cookie.
        if name.startswith("__Host-"):
            item["url"] = ORIGIN
        else:
            item["domain"] = ".myfitnesspal.com"
            item["path"] = "/"
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
    *,
    remint: bool = False,
) -> tuple[dict[str, str], list[dict]]:
    """Open the persistent profile and request the rotation URLs.

    `remint` opens the diary first so a still-valid legacy cookie can receive
    a new NextAuth session cookie. Response bodies are for the liveness check
    only. Callers must not log them. The returned responses are the session
    poll and the auth-token exchange, in that order.
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
            for url in navigation_urls(remint=remint):
                response = page.goto(url, wait_until="load", timeout=30000)
                if url in ROTATION_URLS:
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


def browser_seed() -> dict[str, str] | None:
    """Full cookies.json jar. `MFP_COOKIE` is a single token and is not enough."""
    stored = auth.stored_cookies()
    if stored:
        return stored
    return auth.load_cookies()


def api_cookies_still_valid(cookies: dict[str, str] | None) -> bool:
    """The same exchange as `mfp-mcp auth --check`, without printing the result."""
    if not cookies:
        return False
    try:
        mfp_client.build_client(cookies)
    except Exception:
        # The client error can quote the cookie jar. A failed check is
        # "not valid"; callers explain that without the exception text.
        return False
    return True


def _failure_message(state: str, *, api_ok: bool, reminted: bool) -> str:
    if state == TRANSIENT:
        return (
            "Keepalive could not confirm the session (Cloudflare or a server "
            "error). The saved cookies were left unchanged."
        )
    if state == LIVE:
        return (
            "Keepalive saw a live session but the browser did not return a "
            "session cookie. The saved cookies were left unchanged."
        )
    if api_ok and reminted:
        return (
            "NextAuth session is logged out. The saved cookies still pass the "
            "API check, but visiting the diary did not mint a new session "
            "cookie. The saved cookies were left unchanged."
        )
    if api_ok:
        return (
            "NextAuth session is logged out. The saved cookies still pass the "
            "API check. The saved cookies were left unchanged."
        )
    return (
        "NextAuth session is logged out, and the saved cookies no longer pass "
        "an API check. Run 'mfp-mcp auth' with a fresh "
        "__Secure-next-auth.session-token. The saved cookies were left unchanged."
    )


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
    seed = browser_seed()
    try:
        harvested, responses = _rotate_session(seed)
    except Exception:
        # Playwright errors can include request URLs and cookie values.
        return False, _failure_message(TRANSIENT, api_ok=False, reminted=False)
    state = session_state(responses[0]) if responses else TRANSIENT
    reminted = False
    api_ok = False
    if state == LOGGED_OUT:
        api_ok = api_cookies_still_valid(seed)
        if api_ok:
            reminted = True
            try:
                harvested, responses = _rotate_session(seed, remint=True)
            except Exception:
                # Same as above: do not interpolate the Playwright error.
                return False, _failure_message(TRANSIENT, api_ok=True, reminted=True)
            state = session_state(responses[0]) if responses else TRANSIENT
    if state == LIVE and has_session_cookie(harvested):
        auth.save_cookies(merge_cookies(auth.stored_cookies(), harvested))
        mfp_client.reset()
        message = (
            f"Session refreshed and written to {config.cookies_path()} (mode 0600)."
        )
        if config.cookie_env():
            message += (
                " MFP_COOKIE is set, so a running server keeps using that value. "
                "Unset MFP_COOKIE to pick up this file."
            )
        return True, message
    return False, _failure_message(state, api_ok=api_ok, reminted=reminted)


def keepalive_status_path() -> Path:
    return config.data_dir() / "keepalive.status"


def _read_status() -> dict:
    path = keepalive_status_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_status(
    *,
    last_success: str | None,
    last_error: str | None,
    consecutive: int,
    alert: bool,
) -> None:
    payload = {
        "last_success": last_success,
        "last_error": last_error,
        "consecutive_failures": consecutive,
    }
    if alert:
        payload["alert"] = True
    path = keepalive_status_path()
    text = json.dumps(payload, indent=2) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _configure_line_buffering() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(line_buffering=True)
        except (OSError, ValueError):
            continue


def _emit(ok: bool, message: str) -> None:
    print(message, file=sys.stdout if ok else sys.stderr, flush=True)


def _report(result: tuple[bool, str]) -> int:
    ok, message = result
    _emit(ok, message)
    return 0 if ok else 1


def run_keepalive(
    *,
    loop: bool = False,
    interval: int | None = None,
    max_failures: int | None = None,
) -> int:
    """Refresh once, or until SIGINT/SIGTERM when `loop` is set.

    A failed refresh does not stop the loop. The wait is 2, 5, then 10
    minutes, and after that the normal interval.
    """
    global _stop
    _configure_line_buffering()
    _stop = False
    seconds = DEFAULT_LOOP_SECONDS if interval is None else interval
    previous = _read_status()
    last_success = previous.get("last_success")
    if not isinstance(last_success, str):
        last_success = None
    consecutive = previous.get("consecutive_failures")
    consecutive = consecutive if isinstance(consecutive, int) and consecutive > 0 else 0

    def record(ok: bool, message: str) -> None:
        nonlocal last_success, consecutive
        if ok:
            consecutive = 0
            last_success = _now()
            error = None
        else:
            consecutive += 1
            error = message
        alert = max_failures is not None and consecutive >= max_failures
        _write_status(
            last_success=last_success,
            last_error=error,
            consecutive=consecutive,
            alert=alert,
        )
        if alert:
            print(
                f"keepalive alert: {consecutive} consecutive failures "
                f"({keepalive_status_path()})",
                file=sys.stderr,
                flush=True,
            )

    if not loop:
        ok, message = keepalive_once()
        record(ok, message)
        _emit(ok, message)
        return 0 if ok else 1

    previous_int = signal.signal(signal.SIGINT, _request_stop)
    previous_term = signal.signal(signal.SIGTERM, _request_stop)
    try:
        while not _stop:
            ok, message = keepalive_once()
            _emit(ok, message)
            record(ok, message)
            delay = seconds if ok else failure_backoff(consecutive, seconds)
            if _sleep(delay):
                return 0
        return 0
    finally:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
