# Security review: myfitnesspal-mcp (re-review)

**Review date:** 2026-10-07
**Reviewed tree:** `dougjjj/myfitnesspal-mcp` `main` at `1bb92c6` (fork synced to `Mason-Levyy/myfitnesspal-mcp` **v0.4.1**, package `mfp-mcp`), plus the hardening in this pull request.
**Prior review:** [PR #1](https://github.com/dougjjj/myfitnesspal-mcp/pull/1) against `7faf9da` / `mfp-mcp` 0.3.0. That branch does not apply onto current `main` (diary, food logging, exercise, water, and refresh locking all landed upstream). This document replaces it.
**Method:** Static read of this repo, its tests and fixtures, `pyproject.toml`, GitHub workflows, `server.json`, and the installed `myfitnesspal==2.1.2` and `mcp==1.30.0` sources in a local venv. Unit tests use fixtures only. **No live MyFitnessPal connection, cookies, or account access was used.**

Status words for the old checklist:

| Status | Meaning |
| --- | --- |
| **fixed upstream** | Already true on v0.4.1, before this PR. |
| **fixed in this PR** | Not on upstream v0.4.1; done by the changes that accompany this document. |
| **still open** | Not done. |
| **new** | Not on the PR #1 checklist. Found on v0.4.1 or introduced by how this fork is published. |

---

## Verdict

**Read-only on your own machine, over stdio, is a reasonable personal setup** after you accept the residual risks below. **A LAN Docker host (Unraid) is acceptable only with the extra settings in [LAN / Unraid](#lan--unraid).** It is not safe to publish the HTTP port to the internet, and it is not safe to turn writes on if a model or another person on that network can call the tools.

What you are accepting either way:

- The only credential is a MyFitnessPal browser session cookie. Anyone who copies it can use the account until MyFitnessPal invalidates it.
- Read tools send food, weight, notes, and exercise into the MCP host, and therefore to whatever model provider that host uses.
- There is no official MyFitnessPal API. This process impersonates Chrome's TLS fingerprint and calls private web endpoints.
- `MFP_ALLOW_WRITES` is enforced only inside this process. MyFitnessPal itself does not offer a read-only session.

### Local stdio (the default)

Leave the process on stdio. Do not pass `--http`. Do not set `MFP_ALLOW_WRITES`. Do not set `MFP_AUTOREFRESH`. Store the cookie with `mfp-mcp auth`, not in MCP JSON.

```json
{
  "mcpServers": {
    "myfitnesspal": {
      "command": "uv",
      "args": ["run", "--directory", "/ABS/PATH/TO/myfitnesspal-mcp", "mfp-mcp"],
      "env": {}
    }
  }
}
```

No `MFP_COOKIE` in that file. Writes stay off because that is now the default. Confirm a log or delete call comes back with `Write tools are disabled`.

### LAN / Unraid

Use this only if the MCP client is on another machine and stdio is impossible.

```bash
export MFP_HTTP_TOKEN="$(openssl rand -hex 24)"
export MFP_HTTP_ALLOW_LAN=1
export MFP_HTTP_ALLOWED_HOSTS=192.168.1.20
# do not set MFP_ALLOW_WRITES
# do not set MFP_AUTOREFRESH
mfp-mcp --http --host 0.0.0.0 --port 8484
```

The client must send `Authorization: Bearer <MFP_HTTP_TOKEN>`. `MFP_HTTP_ALLOWED_HOSTS` must be the hostname or IP the client actually puts in the Host header (the Unraid IP or a LAN DNS name). Publish the Docker port on the LAN interface only.

This is still plain HTTP. On a LAN you do not trust, put TLS in front (an authenticating reverse proxy or an identity-aware tailnet). Anyone who can sniff the network, or read the token out of the client config, can call the read tools. They cannot call write tools unless `MFP_ALLOW_WRITES=1` is also set on the server.

---

## What changed between PR #1 and v0.4.1

Upstream added water logging, exercise list/delete, draft-then-confirm food logging, `auth --check`, a refresh lock so parallel tool calls share one browser, a SQLite lock so parallel calls do not read a half-written day, and food-diary URLs that no longer embed the username (`/food/diary?date=`). Dependabot, a private-key pre-commit hook, and `SECURITY.md` landed. None of PR #1's code (opt-in `MFP_READ_ONLY`, directory modes, non-loopback warning) was merged. Writes stayed on by default. HTTP stayed unauthenticated. Auto-refresh still turned on whenever Playwright was installed.

---

## Checklist from PR #1

### Must-do before first live use

| Item | Status | Where it stands |
| --- | --- | --- |
| Decide the blast radius; run read-only if writes are unacceptable | **fixed in this PR** | Write tools are off unless `MFP_ALLOW_WRITES=1`. `MFP_READ_ONLY=1` forces them off anyway. |
| Use a dedicated MyFitnessPal login | **still open** | Operational. The cookie is still full account access. |
| Never put `MFP_COOKIE` in MCP JSON, systemd units, or a world-readable `.env` | **still open** | `server.json` still advertises `MFP_COOKIE` for clients. Prefer `mfp-mcp auth` and `cookies.json`. |
| Paste only `__Secure-next-auth.session-token` | **still open** | `auth.parse_cookie_input` still stores every `name=value` pair from a `Cookie:` header, and `cookies_to_jar` still rewrites each one onto `.myfitnesspal.com`. The auth prompt now tells you to paste only the session cookie. |
| Lock down `cookies.json`, the database, and the config/data directories | **fixed in this PR** | Upstream already chmod'd `cookies.json` to `0600` after `write_text` (a short umask window). This PR creates that file with mode `0600`, chmod's new app directories to `0700`, and chmod's `data.db` to `0600`. A pre-existing `MFP_MCP_DATA_DIR` whose name is not `myfitnesspal-mcp` is not chmod'd. |
| stdio only; do not pass `--http` or port-forward | **fixed upstream** for the default; HTTP is **fixed in this PR** when you do turn it on | `serve` with no flags is stdio, as it was on 0.3.0. `--http` now requires a bearer and stays on `127.0.0.1` unless you opt into a LAN bind. |
| First run with writes disabled | **fixed in this PR** | That is the default. You no longer have to remember `MFP_READ_ONLY=1`. |
| Know how to revoke (password change, delete `cookies.json` and `browser-profile/`) | **still open** | Operational. Nothing in this repo can revoke the session at MyFitnessPal. |
| Install from a tree you reviewed, not an unpinned `uvx mfp-mcp` | **still open** | `uv.lock` is still gitignored. PyPI publishes `mfp-mcp` from upstream. |
| Do not enable `[autorefresh]` on first use | **fixed in this PR** | Playwright being installed is no longer enough. `MFP_AUTOREFRESH=1` is required before auth seeds a profile or a failed call launches Chromium. |
| Assume the model vendor sees food, weight, and notes | **still open** | By design. Read tools still return that data. |

### Nice-to-have

| Item | Status | Where it stands |
| --- | --- | --- |
| Default-on read-only, or require `MFP_ALLOW_WRITES=1` | **fixed in this PR** | `MFP_ALLOW_WRITES=1` opts in. The gate is the first line of every write tool, and a test fails if a new tool is not classified. |
| Allowlist cookies saved to disk (session token only) | **still open** | Full headers are still stored. Playwright still writes back every cookie it would send to `https://www.myfitnesspal.com/`. |
| Cap `MFP_SYNC_DAYS` and `fitness_bulk_export` | **still open** | `sync_days()` is a bare `int(...)`. Export spans are unbounded. `sync_first=true` still force-fetches the whole span. |
| MCP `readOnlyHint` / `destructiveHint` | **fixed in this PR** | Hints are set. They are hints. The gate above is what actually blocks the call. |
| Require `food_id` + `weight_id` for `fitness_log_food` | **still open** | Upstream improved this: an ambiguous query returns a draft instead of logging the top search hit. A pinned food or a single exact name match can still log without ids. |
| Narrow `is_auth_error`; do not launch Playwright on a generic 403 | **still open** | The matcher is still `401\|403\|unauthorized\|forbidden\|csrf\|login\|session\|token`. With auto-refresh opted in, that still launches Chromium. |
| Encrypt the cookie (keychain / libsecret) | **still open** | Plaintext JSON. |
| Commit `uv.lock`; pin publish artifacts by hash | **still open** | Dependabot's comment says the lockfile is intentionally uncommitted. `publish.yml` still downloads `mcp-publisher` from `releases/latest` with no checksum. |
| Refuse a non-loopback `--host` unless explicitly accepted | **fixed in this PR** | Non-loopback needs `MFP_HTTP_ALLOW_LAN=1` and, for `0.0.0.0` / `::`, `MFP_HTTP_ALLOWED_HOSTS`. The bearer is required in every HTTP case. There is no unauthenticated override. |
| Redact exception text | **still open** | `auth`, `auth --check`, `mfp_client.get_client`, and `sync.tolerating_failures` still interpolate `exc`. |
| CI secret scan; pre-commit reject `cookies.json` / `token.txt` | **still open** | Upstream added `detect-private-key` only. `cookies*` was already gitignored. This PR also ignores `token.txt` and `browser-profile/`. That is not a scanner. |
| Validate `MFP_USERNAME` as a simple identifier | **still open** | Food-diary URLs no longer include the username (**fixed upstream**, commit `35d6ee7`). `exercise/diary/{username}` still interpolates `effective_username`. |
| SQLite `0600`, WAL, and a lock | **mixed** | The threading lock is **fixed upstream** (`Store._lock`, plus the sync and refresh locks). `0600` is **fixed in this PR**. `journal_mode=WAL` is **still open**. |

---

## Findings from PR #1

| ID | Was | Status | Notes on v0.4.1 |
| --- | --- | --- | --- |
| F1 | Critical. Session cookie is account takeover; docs invited a full `Cookie:` header and `MFP_COOKIE` in client JSON. | **still open** | Same credential. `cookies.json` is plaintext. `server.json` still lists `MFP_COOKIE` as a client env var (`isSecret: true`, which does not keep it out of the JSON file). |
| F2 | High. Write tools hit real MyFitnessPal with no confirmation and no read-only switch. | **fixed in this PR** | Upstream had added more writers (water, exercise delete) and still had no switch. They now refuse unless `MFP_ALLOW_WRITES=1`. `fitness_modify_food` is still delete-then-add: a failed add can leave the original food gone. That part is **still open**. |
| F3 | High. Read tools and `fitness_bulk_export` send health data to the model, with no span cap. | **still open** | By design for reads. The export cap was not added. |
| F4 | High. `--http` has no application auth. Default bind was already `127.0.0.1`. | **fixed in this PR** | Bearer required. Loopback by default. Host allowlist is explicit (see DNS rebinding below). |
| F5 | High. A pasted `Cookie:` header is stored and re-domained to `.myfitnesspal.com`. Refresh persists every harvested cookie. | **still open** | `parse_cookie_input`, `cookies_to_jar`, and `_visit_and_harvest` are unchanged. |
| F6 | Medium. Cookie chmod after write; directories and the database inherited the umask; the Chromium profile was not `0700`. | **fixed in this PR** | See the file-mode row above. The profile directory is chmod'd `0700` once it exists. |
| F7 | Medium. `[autorefresh]` launches headless Chromium and writes cookies back. | **fixed in this PR** for the default | Off unless `MFP_AUTOREFRESH=1`. When you opt in, the profile is still a second copy of the session. |
| F8 | Medium. Auth-error regex is broad and can launch the browser on a Cloudflare 403 or a missing CSRF tag. | **still open** | Tests still treat `HTTP 403` and `csrf token` as auth failures. |
| F9 | Medium. Every new client calls `/user/auth_token?refresh=true`. | **still open** | `CurlCffiClient.__init__` still calls `_get_auth_data()`. Two processes sharing one cookie file can still race. |
| F10 | Medium. Floating deps, no lockfile, `mcp-publisher` fetched as `latest` with no checksum. `browser_cookie3` and `cloudscraper` come in via `myfitnesspal`. | **still open** | `myfitnesspal==2.1.2` and `curl-cffi>=0.15,<0.16` stay pinned as before. `lxml` moved from `<6` to `<7` (**new**, looser). `mcp` resolved to `1.30.0` in this review and is still `>=1.10,<2`. `CurlCffiClient` does **not** call `Client.__init__`, so the happy path does not call `browser_cookie3.load()` or build a cloudscraper session. Both packages are still installed. |
| F11 | Medium. Chrome TLS impersonation and unofficial XHR writes. | **still open** | Documented purpose. `MFP_IMPERSONATE` defaults to `chrome`. Not a supported API. Account flags and breakage are still possible. |
| F12 | Medium. Unbounded `MFP_SYNC_DAYS` and export span. | **still open** | Unchanged. |
| F13 | Low. Exception strings can include URL or body snippets. | **still open** | `auth --check` now prints `rejected ({exc})` as well. Cookie values are still not logged on purpose. `getpass` still hides a TTY paste. |
| F14 | Low. `token.txt` was easy to commit. | **fixed in this PR** for the ignore rule | `.gitignore` now includes `token.txt` and `browser-profile/`. `cookies*` and `.env` were already ignored. There is still no pre-commit hook that rejects those names. |
| F15 | Low. One SQLite connection, `check_same_thread=False`, no lock. | **fixed upstream** | `Store` methods take an `RLock`. Sync and refresh have their own locks (PRs #21 and #22 upstream). `check_same_thread=False` remains. WAL is still off. |
| F16 | Low. Username interpolated into a diary path (`urljoin` SSRF if it looks like a URL). | **still open** on exercise | Food diary path no longer includes the username (**fixed upstream**). Exercise reads and deletes still use `exercise/diary/{effective_username}`. |
| F17 | Info. No live secrets in the tree; default HTTP host was loopback. | **fixed upstream** | Still true. Test tokens are fakes. This PR does not add a secret scanner. |
| F18 | Info. Trusted Publishing to PyPI and the MCP registry under the upstream name. | **still open** | `server.json` `name` is still `io.github.Mason-Levyy/mfp-mcp`. This fork should not publish. See the new publish-workflow note below. |

---

## New on v0.4.1

| Item | Status | Notes |
| --- | --- | --- |
| Water, exercise delete, local pins, local drafts, and local feel notes are write tools | **fixed in this PR** | They did not exist, or were not covered, at the 0.3.0 review. All of them go through the same gate. `fitness_draft_food` does not change MyFitnessPal, but it writes a local draft whose only purpose is a later log, so it is treated as a write. `fitness_search_food` stays available. |
| `lxml` upper bound loosened from `<6` to `<7` | **new** | Still a range, not a hash pin. |
| `publish.yml` runs on every `pyproject.toml` push to `main`, not only version bumps | **new** | It creates `v<version>` if that tag is missing, then publishes with `pypa/gh-action-pypi-publish` and an unpinned `mcp-publisher` tarball. A dependency-only edit on this fork can try to release `mfp-mcp` if the tag is absent and the `pypi` environment exists. Do not merge `pyproject.toml` edits onto this fork's `main` until that workflow is disabled here. |
| HTTP bearer is a shared static secret, not OAuth login | **new** | The MCP SDK requires `AuthSettings` beside a `TokenVerifier`, so the process also serves the protected-resource metadata document. Nothing in this repo issues OAuth tokens. Clients must send the bearer. The secret travels in cleartext on plain HTTP. |
| DNS-rebinding protection was an SDK side effect, not this repo's configuration | **fixed in this PR** | `FastMCP("myfitnesspal")` on `mcp>=1.23` (this review resolved `1.30.0`) turns Host checks on because the SDK default host is `127.0.0.1`. The dependency range still allows `1.10`–`1.22`, where that default is off ([PYSEC-2026-1617](https://osv.dev/vulnerability/PYSEC-2026-1617)). `--host` used to change the bind without rewriting the allowlist. This PR sets `TransportSecuritySettings` explicitly for the hosts it is willing to serve. |
| `auth --check` contacts MyFitnessPal and prints the exception | **new** | Useful, and another place an HTTP library exception can land in the host's logs. It does not print the cookie on purpose. |

---

## Areas asked about in this re-review

### Credential and cookie handling

The session cookie `__Secure-next-auth.session-token` is the only secret. `mfp-mcp auth` reads it with `getpass` on a TTY, or from stdin if there is no TTY. `MFP_COOKIE`, when set, wins over the file. The file is `<platformdirs user config>/myfitnesspal-mcp/cookies.json` with `cookies` and an optional `username`. This PR creates it mode `0600`.

Logging does not print the cookie value. It does print the cookie **path** after a successful auth, the auto-refresh profile path, and exception strings. stderr is the MCP log stream in stdio mode.

`CurlCffiClient` never calls `myfitnesspal.Client.__init__`, so it does not read other browsers' cookie jars via `browser_cookie3`.

### curl_cffi impersonation

`mfp_client.CurlCffiClient` builds `curl_cffi.requests.Session(impersonate=MFP_IMPERSONATE or "chrome")` and puts the cookie jar on that session. The pinned window is `curl-cffi>=0.15,<0.16`. A minor bump changes the TLS fingerprint MyFitnessPal sees; Dependabot is told not to bump it because CI never talks to the network. This is deliberate WAF evasion. It is not telemetry.

### Auto-refresh and the stored profile

`refresh.py` launches headless Chromium with a persistent profile at `<data dir>/browser-profile`, visits `https://www.myfitnesspal.com/`, and saves every cookie for that URL. Upstream did this whenever Playwright imported. This PR requires `MFP_AUTOREFRESH=1` as well. The profile is chmod'd `0700` when it exists. Treat it as a second session secret. Do not snapshot it.

### Outbound network endpoints

No analytics host is called by this package. The README's mention of `POST /stats` with a `water_logged` event describes the MyFitnessPal web app. `diary.log_water` does not send it. A search of this repo, `myfitnesspal` 2.1.2, and `mcp` 1.30.0 found no Sentry, PostHog, or Segment calls.

Runtime calls, all on the session cookie or the bearer minted from it:

| When | Method and URL |
| --- | --- |
| Every new client | `GET https://www.myfitnesspal.com/user/auth_token?refresh=true` |
| Every new client | `GET https://api.myfitnesspal.com/v2/users/{user_id}?...` (profile; failure falls back to `MFP_USERNAME`) |
| Search and drafts | `GET https://www.myfitnesspal.com/food/search?search=` |
| Search macros | `GET https://api.myfitnesspal.com/v2/foods/{id}?...` |
| Log / modify food | `POST https://www.myfitnesspal.com/food/add` |
| Read a day, sync | `GET https://www.myfitnesspal.com/food/diary?date=` |
| Delete food | `POST https://www.myfitnesspal.com/food/remove/{id}` |
| Weight write | `POST https://api.myfitnesspal.com/v2/measurements` |
| Weight sync | `GET https://www.myfitnesspal.com/measurements/edit?...` (library `get_measurements`) |
| Water | `GET` and `POST https://www.myfitnesspal.com/food/water` |
| Notes | `GET` and `POST https://www.myfitnesspal.com/food/note` |
| Exercise | `GET https://www.myfitnesspal.com/exercise/diary/{username}?date=` |
| Delete exercise | `POST https://www.myfitnesspal.com/exercise/remove/{id}` |
| Auto-refresh, only if opted in | Headless Chromium `GET https://www.myfitnesspal.com/` |

`sync.refresh_day` also touches the diary page's water field, which issues `GET /food/water`. CI and publish workflows call GitHub, PyPI, and `astral.sh`. Those do not run inside the MCP process.

### Telemetry

None in this server. The only "telemetry" string in the tree is the README note that this code does not emit MyFitnessPal's `water_logged` stats event.

### Write tools and the read-only gate

Off unless `MFP_ALLOW_WRITES` is `1`, `true`, `yes`, or `on`. `MFP_READ_ONLY` set the same way wins.

Blocked: `fitness_draft_food`, `fitness_log_food`, `fitness_clear_food_pin`, `fitness_delete_food`, `fitness_modify_food`, `fitness_log_weight`, `fitness_log_water`, `fitness_delete_exercise`, `fitness_log_note`, `fitness_log_feel`.

Not blocked: `fitness_get_day`, `fitness_search_food`, `fitness_list_food_pins`, `fitness_get_exercise`, `fitness_get_exercise_entries`, `fitness_get_note`, `fitness_get_trends`, `fitness_bulk_export`.

Read tools still fill the local SQLite cache. That is a cache write, not a diary write. `tests/test_read_only.py` calls every write tool with the network client and the store rigged to explode, and expects `ReadOnlyError` first. It also fails if a newly registered tool is missing from one of the two sets.

The gate is on the MCP tools. Importing `diary.push_food` yourself bypasses it. That is not an MCP path.

### HTTP transport

- Default command is `serve` with `--http` false. Default `--host` is `127.0.0.1`, default port `8484`.
- `--http` exits before listen if `MFP_HTTP_TOKEN` is missing or shorter than 16 characters. The token is not printed.
- The SDK's bearer middleware returns 401 without a matching `Authorization: Bearer` value. Required scope is `mfp`, which only this process's verifier grants.
- DNS-rebinding: `enable_dns_rebinding_protection=True`. Loopback allows Host `127.0.0.1`, `localhost`, and `[::1]` on any port. A `Host: evil.example` request is 421 even with the right bearer. A missing `Origin` is allowed (normal non-browser clients). A present `Origin` must be `http` or `https` on an allowed host.
- `0.0.0.0` is refused unless `MFP_HTTP_ALLOW_LAN=1` and `MFP_HTTP_ALLOWED_HOSTS` names the hosts clients will send. A specific LAN IP is added to that allowlist automatically.

### Dependencies, workflows, PyPI

| Piece | What this review saw |
| --- | --- |
| Direct deps | `mcp>=1.10,<2` (resolved `1.30.0`), `myfitnesspal==2.1.2`, `curl-cffi>=0.15,<0.16` (resolved `0.15.0`), `lxml>=5,<7` (resolved `5.4.0`), `platformdirs>=4`. Optional `playwright>=1.40`. Dev: pytest, ruff, pre-commit. No hashes, no lockfile. |
| Transitive | `browser-cookie3==0.20.1`, `cloudscraper==1.2.71`, plus their crypto stack. Not used on the curl_cffi path. |
| CI | `ci.yml`: ruff, pytest on Python 3.10 and 3.13, version consistency between `pyproject.toml` and `server.json`. `contents: read`. Actions are pinned to major tags (`checkout@v7`, `setup-uv@v7`), not SHAs. |
| Dependabot | Monthly, for Actions and pip. Ignores `myfitnesspal`, `curl-cffi`, and `mcp` majors on purpose. |
| Publish | `publish.yml`: on `pyproject.toml` changes to `main`, tag `v<version>` if missing, then Trusted Publishing to PyPI (`pypa/gh-action-pypi-publish@release/v1`, OIDC) and the MCP registry. `mcp-publisher` is `curl \| tar` of the latest GitHub release, unpinned. |
| Package identity | PyPI project `mfp-mcp`. `server.json` repository and name still point at `Mason-Levyy`. This fork should not ship that. |

This was not a vulnerability scan of the resolved set. No advisory database was queried beyond the known MCP DNS-rebinding note above.

---

## Hardening in this PR

| Change | Behavior |
| --- | --- |
| `MFP_ALLOW_WRITES` | Unset: every write tool raises `ReadOnlyError` before it touches MyFitnessPal or the store. |
| `MFP_READ_ONLY=1` | Forces the gate closed. |
| stdio | Still the default. Covered by a parser test. |
| `MFP_HTTP_TOKEN` | Required for `--http`. Compared with `secrets.compare_digest`. Not logged. |
| Bind | `127.0.0.1` unless `MFP_HTTP_ALLOW_LAN=1`. Wildcard binds need `MFP_HTTP_ALLOWED_HOSTS`. |
| Host check | Explicit `TransportSecuritySettings`, including on loopback, so it does not depend on the SDK default. |
| `MFP_AUTOREFRESH` | Required before Playwright is used. |
| File modes | `0700` directories this app creates, `0600` cookie file and database, `0700` browser profile when present. |
| Tool hints | `readOnlyHint` / `destructiveHint` match the gate. Hosts may ignore hints. |

Not done, on purpose: cookie allowlist, export caps, exception redaction, username validation, keychain storage, a lockfile, pinning `mcp-publisher`.

---

## After a suspected leak

1. Unset `MFP_COOKIE` and `MFP_HTTP_TOKEN`. Delete `cookies.json` and `browser-profile/`.
2. Change the MyFitnessPal password and sign out other sessions if the product offers it.
3. Rotate any password reused on that account.
4. Run `mfp-mcp auth` again only on a machine you trust.

---

## Out of scope

This review did not log in to MyFitnessPal, exercise Playwright or Cloudflare against the live site, audit the internals of `curl_cffi`, Chromium, or the MCP SDK beyond the calls this repo makes, or run a CVE database against the lockfile (there is no lockfile). MyFitnessPal's terms are a legal question, not a finding in this document.
