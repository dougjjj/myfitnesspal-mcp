# Security review: myfitnesspal-mcp

**Review date:** 2026-09-10  
**Reviewed tree:** `dougjjj/myfitnesspal-mcp` at `7faf9da` (fork of `Mason-Levyy/myfitnesspal-mcp`, package `mfp-mcp` 0.3.0)  
**Method:** Read-only static review of this repo, its tests/fixtures, `pyproject.toml` / CI, and the pinned `myfitnesspal==2.1.2` client API. Unit tests may be run without credentials. **No live MyFitnessPal connection, cookies, or account access was used.**

This document is for Doug, before he points the server at a real MyFitnessPal account.

---

## Executive summary

**Do not run this against a live MyFitnessPal account until the [must-do checklist](#1-must-do-before-first-live-use) is done.**

After that checklist, it is **conditionally acceptable for personal, local, stdio-only use** if you explicitly accept the residual risks below. It is **not** safe to treat as a normal “API integration”: there is no official MFP API, the credential is a full browser session cookie, and every MCP tool that talks to MFP runs with that cookie’s full account power.

| Verdict | When |
| --- | --- |
| **Do not run until** | Session cookie is kept out of MCP JSON / shell history / synced configs; HTTP mode is unused; you understand the model can **read and write** the real diary; local secret files are `0600` / dirs `0700`; you have a revoke plan (MFP password change / logout-all / new cookie). |
| **Safe-enough to run after** | Must-do checklist complete, **and** first live use is with `MFP_READ_ONLY=1` (added in this PR) or a dedicated/throwaway MFP account, **and** the only transport is local stdio. |
| **Still do not** | Bind `--http` to a non-loopback address, put `MFP_COOKIE` in `claude_desktop_config.json` / committed `.env`, share the MCP with other people, or assume Cloudflare-bypass + unofficial writes are ToS-safe. |

Highest-impact issues, in order:

1. **The session cookie is account takeover.** `__Secure-next-auth.session-token` is a bearer session, stored in plaintext `cookies.json` or `MFP_COOKIE`. Docs encourage pasting the entire `Cookie:` header into a prompt or a file.
2. **The MCP client is fully privileged.** There is no confirmation step. An LLM (or anything that can call the tools) can log, delete, and rewrite diary entries, change weight, and overwrite the MFP daily note. Prompt injection against the host agent is therefore a **write** to your real account.
3. **Health data is designed to leave the machine.** `fitness_get_day`, `fitness_get_trends`, `fitness_bulk_export`, notes, and exercise all return diary/weight/feel data to the model. That is the product — and it is also exfiltration into the LLM provider’s context/logs.
4. **HTTP mode has no authentication** and this process uses the official `mcp` SDK FastMCP path (`cli.py` → `mcp.run(transport="streamable-http")`), which does not add a bearer check or Host/Origin guard here.

This PR does **not** rewrite authentication. It documents the risks, tightens file permissions, refuses a silent non-loopback HTTP bind warning, ignores local secret filenames, and adds an **opt-in** `MFP_READ_ONLY=1` guard so first live use can be read-only without changing default behavior.

---

## Architecture and trust boundaries

```
 MCP host (Claude / Cursor / other)
   │  stdio (default)  or  streamable HTTP :8484 (no auth)
   ▼
 myfitnesspal-mcp process
   │  secrets: MFP_COOKIE  or  <config_dir>/cookies.json
   │  cache:   <data_dir>/data.db
   │  optional: <data_dir>/browser-profile  (Playwright Chromium)
   ▼
 curl_cffi Session (Chrome TLS fingerprint)  +  python-myfitnesspal.Client
   │  GET https://www.myfitnesspal.com/user/auth_token?refresh=true
   │      → in-memory Bearer access_token + user_id
   ▼
 MyFitnessPal web app + unofficial XHR
   /food/search  /food/add  /food/remove  /food/note
   /food/diary/<username>  /v2/measurements  diary/exercise HTML
```

### What is trusted

| Principal | What it can do with this server |
| --- | --- |
| **Whoever can invoke MCP tools** | Read cached + live diary, weight, exercise, notes; with default settings, **mutate the live MFP account**. |
| **Whoever can read `cookies.json`, `MFP_COOKIE`, or the Playwright profile** | Impersonate you on MyFitnessPal until the session is revoked. |
| **Whoever can reach the HTTP port** | Same as the MCP host. No login. Default bind is `127.0.0.1` (any local process). |
| **MyFitnessPal HTML/JSON** | Parsed by `lxml` and `python-myfitnesspal`. A hostile or unexpected page can break parsing; it is not treated as untrusted input beyond that. |
| **PyPI / `uvx mfp-mcp`** | Install-time code execution as you. `uv.lock` is gitignored, so `mcp`, `curl-cffi`, `lxml`, and `platformdirs` float within ranges. `myfitnesspal` is pinned to `2.1.2`. |

### Auth map (this repo, not generic)

| Mechanism | Where | Role |
| --- | --- | --- |
| `__Secure-next-auth.session-token` | Pasted into `mfp-mcp auth`, or `MFP_COOKIE`, or `cookies.json` | **Only real secret.** NextAuth session cookie. |
| Full `Cookie:` header | `auth.parse_cookie_input` | Accepted and stored **as a dict of every `name=value` pair**, not just the session cookie. |
| `MFP_USERNAME` / `cookies.json` `"username"` | `config.username_env`, `auth.saved_username` | Not a secret. Used when MFP’s profile endpoint 500s so diary URLs still work. |
| `cookies.json` | `platformdirs.user_config_dir("myfitnesspal-mcp")` / `cookies.json` (`~/.config/myfitnesspal-mcp/cookies.json` on Linux) | Plaintext JSON `{"cookies": {...}, "username": "..."}`. `save_cookies` chmod’s the **file** to `0600` after write. Parent dir was created with default umask (`0755` typical). |
| Playwright profile | `data_dir()/browser-profile` | Persistent Chromium user-data dir. Seeded with whatever cookie dict `auth` stored; on refresh, **all** cookies Playwright would send to `https://www.myfitnesspal.com/` are written back to `cookies.json`. |
| Bearer `access_token` | Memory only, from inherited `Client._get_auth_data()` | `GET /user/auth_token?refresh=true` on **every** `build_client()` / `get_client()` miss. Sent as `Authorization: Bearer …` on diary XHR (`diary.api_headers`). |
| CSRF / authenticity tokens | Scraped from MFP HTML per request | Page-bound write CSRF. Not stored. |
| `MFP_IMPERSONATE` | Env, default `chrome` | `curl_cffi` JA3/TLS fingerprint name. Not a secret. Exists to pass Cloudflare. |
| `python-myfitnesspal` `browser_cookie3` | Transitive dependency | **Not used on the happy path** — `CurlCffiClient` always passes a jar. Upstream `Client()` *without* a jar would load cookies from the local browser for `myfitnesspal.com`. Do not instantiate the upstream client that way. |
| Passwords / OAuth / official API keys | — | **None.** MFP killed password login for this stack; that is why the cookie exists. |

There is no browser-scraping of *other* sites. Playwright’s `page.goto` target is the hardcoded `https://www.myfitnesspal.com/` (`refresh.py`). Search queries are `urllib.parse.quote`’d onto that same origin. No `eval`, `subprocess`, or unsanitized SQL identifiers (nutrition/trend columns are allowlisted in `store.py`).

### Local data (health + secrets)

| Path | Contents | Default perms (before this PR) |
| --- | --- | --- |
| `<config_dir>/cookies.json` | Session cookies + username | File `0600` after `save_cookies`; dir typically `0755` |
| `<data_dir>/data.db` | Nutrition, diary lines, MFP notes, local feel notes, sync cursor | SQLite default (usually `0644`) |
| `<data_dir>/browser-profile/` | Full Chromium profile (cookies, localStorage, cache) | Whatever Chromium creates |
| MCP client env / JSON | Optional `MFP_COOKIE` | Whatever the host file is (often world-readable) |

`MFP_MCP_DATA_DIR` relocates the DB and Playwright profile. It does not relocate `cookies.json`.

---

## Findings

| ID | Severity | Location | Issue | Impact | Recommended fix |
| --- | --- | --- | --- | --- | --- |
| F1 | **Critical** | `auth.py` (`SESSION_COOKIE`, `parse_cookie_input`, `run_auth_flow`); `config.cookie_env`; README Authentication; `server.json` `MFP_COOKIE` | The only credential is a NextAuth **session cookie**, equivalent to being logged in. Docs tell you to copy it from DevTools and accept a full `Cookie:` header. `MFP_COOKIE` is advertised for MCP client env, which usually means a JSON file on disk (and sometimes cloud-synced). | Anyone who sees the value can use your MFP account (diary, weight, notes) from another machine until logout/revoke. Pasting into chat, shell history, or a shared config is a leak. | Never put `MFP_COOKIE` in MCP JSON. Use `mfp-mcp auth` once, keep `cookies.json` at `0600` on an unshared disk. Treat the cookie like a password: don’t paste it into Slack/email/tickets. After any suspected leak, change the MFP password and sign out other sessions, then re-`auth`. Prefer a password manager / `0600` file over env. |
| F2 | **High** | `server.py` `fitness_log_food`, `fitness_delete_food`, `fitness_modify_food`, `fitness_log_weight`, `fitness_log_note`; `diary.py` `push_food` / `delete_food` / `modify_food` / `set_weight` / `set_note` | Write tools hit **real** MFP endpoints with no confirmation, allow-list, or (until this PR) read-only switch. `push_food` without `food_id` logs the **top search hit**. `modify_food` deletes first; a failed add leaves the food gone. | Prompt injection, a confused model, or a second local process can delete meals, log wrong food, alter weight history, or overwrite notes. | First live use: `MFP_READ_ONLY=1`. Longer term: split a read-only server entrypoint, add MCP `readOnlyHint` / `destructiveHint`, require `food_id`+`weight_id` for logs, and confirm destructive calls in the host. |
| F3 | **High** | `server.py` `fitness_get_day`, `fitness_get_trends`, `fitness_bulk_export`, `fitness_get_note`, `fitness_get_exercise`; `store.export_range` | Tools return nutrition, food names, MFP notes, and local feel notes to the MCP host. `fitness_bulk_export` has **no max range**. `sync_first=True` plus a wide range force-polls MFP one day at a time (`sync.poll(..., force=True)`). | Designed data path into the LLM (and any logging the host/provider does). A long export is a bulk dump of health data. `sync_first` can also hammer MFP. | Assume the model vendor sees whatever the tools return. Don’t enable this MCP on a shared or logged agent. Cap export/sync span in code (e.g. 90 days). Keep `sync_first` off unless you need it. |
| F4 | **High** | `cli.py` `--http` / `--host` / `--port`; README “Remote / HTTP mode” | Streamable HTTP is served with **no application auth**. Bind default is `127.0.0.1:8484` (good). `--host 0.0.0.0` (or a LAN IP) is accepted. Official `mcp.server.fastmcp` is used; this repo does not enable Host/Origin protection or a bearer. | Any client that can open the port gets the same write/read tools and thus the live account. Localhost is still reachable by other processes/users on the box (and, depending on browser + CORS, is in the DNS-rebinding conversation). | Do not use `--http` for personal MFP. If you must, bind loopback only, put an authenticating proxy in front, and never port-forward. This PR prints a warning when the bind is not loopback. |
| F5 | **High** | README “Pasting the entire Cookie: header”; `auth.parse_cookie_input`; `mfp_client.cookies_to_jar`; `refresh._visit_and_harvest` | Every cookie in a pasted header is stored and then **re-domained to `.myfitnesspal.com`** (`secure=True`). Playwright later harvests **all** cookies for the MFP URL and `save_cookies`’s the whole dict. | If you paste the wrong site’s `Cookie:` header (or a header that mixed third-party cookies), those values are written to disk **and sent to MyFitnessPal**. Refresh can persist Cloudflare / analytics cookies you did not intend to copy. | Paste **only** `__Secure-next-auth.session-token`. Code fix: allowlist that name (plus any cookie you have proven is required, e.g. `cf_clearance`). Do not rewrite foreign cookies onto the MFP domain. |
| F6 | **Medium** | `config.config_dir` / `data_dir`; `auth.save_cookies`; `store.Store.__init__`; `refresh.profile_dir` | Cookie file is chmod’d **after** `write_text` (short default-umask window; existing `0644` files stay loose until the next save). Config/data dirs used `mkdir` without `0700`. SQLite `data.db` and the Chromium profile inherited umask. | Local users/backups/sync agents can read health history and, if the cookie file was ever copied without `0600`, the session. | This PR: `0700` on app dirs we create, `0600` cookie write + chmod, `0600` on the DB file. After pull: `chmod 700` the config/data dirs and `chmod 600` `cookies.json` / `data.db`. Exclude them from Dropbox/iCloud. |
| F7 | **Medium** | `refresh.py`; README Auto-refresh; optional extra `playwright` | `[autorefresh]` launches **headless Chromium** with a persistent profile and your session cookies, then writes harvested cookies back. Profile path is printed on success. | Extra malware/XSS surface (a real browser engine). Profile on disk is a second copy of the session. A compromised or unexpected MFP page runs in that profile. | Skip `[autorefresh]` until you need it. If you use it, treat `browser-profile/` as secret (`0700`), don’t snapshot it, and delete it when done. Re-`auth` after. |
| F8 | **Medium** | `mfp_client.is_auth_error`; `server.run_with_refresh`; tests treat “csrf token” as auth | Auth detection is a regex: `401\|403\|unauthorized\|forbidden\|csrf\|login\|session\|token`. A missing CSRF meta tag or a Cloudflare 403 looks like “session expired” and triggers Playwright refresh. | Unnecessary browser launch; masking of real outages; refresh can overwrite `cookies.json` with a partial harvest. | Narrow the matcher (status + `MyfitnesspalLoginError` / `NotConnectedError` only). Don’t treat “csrf token” HTML changes as expiry. |
| F9 | **Medium** | Inherited `myfitnesspal.Client._get_auth_data` (`/user/auth_token?refresh=true`) via `CurlCffiClient.__init__` | Every new client (startup, post-`reset()`, after refresh) asks MFP to **refresh** an auth token. | Extra token issuance; if MFP ever invalidates the previous token, multiple processes sharing `cookies.json` can race. | Cache the client (already done). Avoid bouncing `reset()`. Document that two machines sharing one cookie file is unsupported. |
| F10 | **Medium** | `pyproject.toml` ranges; `.gitignore` `uv.lock`; `.github/workflows/publish.yml` `mcp-publisher` `curl …/latest/…` | Runtime deps other than `myfitnesspal` are unpinned ranges. `uvx mfp-mcp` pulls current PyPI. Publish job downloads **latest** `mcp-publisher` tarball with no checksum. Transitive `myfitnesspal` deps include `browser_cookie3` and `cloudscraper`. | Supply-chain: a bad `mcp` / `curl-cffi` / publisher binary release becomes your process. `browser_cookie3` is a browser-cookie reader you do not need on the happy path. | Commit a lockfile for dev. Pin hashes for release. Verify `mcp-publisher`. Don’t call upstream `Client()` without a jar. Review each `uvx` upgrade. |
| F11 | **Medium** | README / `auth.py` Cloudflare notes (`MFP_IMPERSONATE`, “residential IP”); unofficial XHR writes in `diary.py` | The integration **impersonates Chrome’s TLS fingerprint** and replays private web endpoints. README already says unofficial / use at your own risk. | Account lock, ToS action, or sudden breakage when MFP changes HTML or WAF. Not a “supported API.” | Use a dedicated MFP account if you can. Don’t run from shared/cloud IPs you don’t control. Expect breakage. |
| F12 | **Medium** | `config.sync_days`; `server.parse_range`; `fitness_bulk_export(sync_first=…)` | `MFP_SYNC_DAYS` is `int(...)` with no bounds. Date ranges are unbounded. | A typo or a model-chosen range can issue thousands of MFP GETs and fill the local DB. | Clamp lookback (e.g. 1–90). Reject export spans over a fixed max. |
| F13 | **Low** | `auth.run_auth_flow` (`Those cookies didn't authenticate: {exc}`); `mfp_client.get_client` (`auth failed: {exc}`); `sync.tolerating_failures` logs `exc` | Exception strings from HTTP libraries can include URLs or body snippets. Cookie values are not intentionally logged; `getpass` hides TTY paste. | Low odds of secret-in-logs unless an exception echoes a header. stderr from MCP stdio is often captured by the host. | Never log cookies, `Authorization`, or raw `MFP_COOKIE`. Redact `exc` in user-facing strings. |
| F14 | **Low** | `auth.py` “`myfitnesspal-mcp auth < token.txt`”; `.gitignore` (pre-PR) | Docs suggest a token file. `cookies*` and `.env` were ignored; `token.txt`, `*.sqlite`, and `browser-profile/` were not. | A leftover `token.txt` in the repo cwd is easy to `git add`. | Don’t keep token files. If you pipe, use a `0600` file and shred it. This PR gitignores the obvious names. |
| F15 | **Low** | `store.Store` `check_same_thread=False`; no file lock | Concurrent MCP calls share one SQLite connection. | Corruption / lost feel-notes under parallel tools, not remote RCE. | One writer queue, or `sqlite3` check + timeout, if you see races. |
| F16 | **Low** | `diary.py` `food/diary/{effective_username}`; `MFP_USERNAME` | Username is interpolated into a path. `urljoin` + a username that looks like an absolute URL would be a classic SSRF footgun. Source is your env or MFP’s profile JSON. | Not attacker-controlled in the intended setup. Still don’t set `MFP_USERNAME` to a URL. | Allowlist `^[A-Za-z0-9._-]+$` before building diary URLs. |
| F17 | **Info** | Repo + tests + fixtures | No live cookies, passwords, or account dumps in tree. Test tokens are `abc123` / `fake-token`. SQL identifiers are allowlisted. Playwright URL is fixed. Default HTTP host is loopback. README already warns not to expose HTTP to the internet. | Good baseline hygiene. | Keep it that way; add a secret scan in CI if the fork grows. |
| F18 | **Info** | `.github/workflows/publish.yml` | Trusted Publishing to PyPI + MCP registry on version bump. This fork’s `server.json` / README still name `Mason-Levyy`. | If you publish from the fork, you are shipping installable code that other people may `uvx`. | Don’t publish until you own the release identity and have reviewed the same issues for *your* users. |

---

## Prioritized hardening checklist

### 1. Must-do before first live use

- [ ] **Decide the blast radius.** This process can change the real diary. If that is unacceptable, stop here or only ever run with `MFP_READ_ONLY=1`.
- [ ] **Use a dedicated MFP login if you can** (not the same password you use elsewhere). Session cookie theft is account theft.
- [ ] **Never put `MFP_COOKIE` in** `claude_desktop_config.json`, Cursor MCP JSON, systemd units checked into git, or a world-readable `.env`. Prefer `mfp-mcp auth` → `cookies.json`.
- [ ] **Paste only** `__Secure-next-auth.session-token` (not a full `Cookie:` header, not cookies from another site).
- [ ] **Lock down files** (also done automatically on new dirs/files after this PR):

  ```bash
  chmod 700 ~/.config/myfitnesspal-mcp ~/.local/share/myfitnesspal-mcp
  chmod 600 ~/.config/myfitnesspal-mcp/cookies.json ~/.local/share/myfitnesspal-mcp/data.db
  ```

  macOS paths are under `~/Library/Application Support/` (data) and `~/Library/Application Support/` / `~/Library/Preferences`-style config via `platformdirs` — run `python -c "from myfitnesspal_mcp import config; print(config.cookies_path(), config.database_path())"` after install if unsure.

- [ ] **stdio only.** Do not pass `--http`. Do not `tailscale serve` / port-forward this process.
- [ ] **First run with writes disabled:**

  ```bash
  export MFP_READ_ONLY=1
  ```

  Add the same env to the MCP server definition. Confirm the model can *read* a day and that a log/delete call is rejected.

- [ ] **Know how to revoke.** MFP password change + sign out of other sessions (or wait for ~30 day expiry) + delete `cookies.json` and `browser-profile/`. Then re-`auth` only if you still want the integration.
- [ ] **Install from a tree you reviewed** (`uv sync` from this git sha), not an unpinned `uvx mfp-mcp` from PyPI, until you trust a release.
- [ ] **Don’t enable `[autorefresh]`** on first use. Re-run `auth` when the session dies.
- [ ] **Assume the LLM vendor sees your food, weight, and notes.** Don’t log anything in MFP notes you wouldn’t put in the chat transcript.

### 2. Nice-to-have (code / ops, after you are running)

- [ ] Implement a **default-on read-only entrypoint** (or invert `MFP_READ_ONLY` so writes require `MFP_ALLOW_WRITES=1`).
- [ ] Allowlist cookies persisted to disk (session token only, unless CF cookies are proven required).
- [ ] Cap `MFP_SYNC_DAYS` and `fitness_bulk_export` span; ignore `sync_first` above the cap.
- [ ] Add MCP tool annotations (`readOnlyHint`, `destructiveHint`) and host-side confirmation for deletes.
- [ ] Require `food_id` + `weight_id` for `fitness_log_food` (search-then-log only).
- [ ] Narrow `is_auth_error`; never launch Playwright on a generic 403.
- [ ] Encrypt-at-rest or OS keychain for the session cookie (macOS Keychain / libsecret). Plaintext JSON is a stopgap.
- [ ] Commit `uv.lock`; pin CI and publish artifacts by hash.
- [ ] Refuse `--host` that is not loopback unless `--i-accept-unauthenticated-http` is set.
- [ ] Redact exceptions; never print cookie paths into shared logs if the path contains a username you care about (usually fine).
- [ ] CI secret scan; pre-commit hook rejecting `cookies.json` / `token.txt`.
- [ ] Validate `MFP_USERNAME` as a simple identifier.
- [ ] SQLite `0600` + `pragma journal_mode=WAL` + a lock if you see concurrent writes.

---

## Suggested safe setup pattern

### Secrets

1. Log in to MyFitnessPal in your **everyday browser**.
2. Copy **only** `__Secure-next-auth.session-token`.
3. On the same machine, in a real TTY (so `getpass` hides input):

   ```bash
   uv run mfp-mcp auth
   ```

4. Confirm the file mode:

   ```bash
   ls -l "$(python -c "from myfitnesspal_mcp.config import cookies_path; print(cookies_path())")"
   # expect -rw------- (0600)
   ```

5. **Do not** export `MFP_COOKIE`. If a client *requires* an env var, point it at a wrapper that reads the `0600` file — do not embed the token in JSON.

6. If you already pasted a full `Cookie:` header, delete `cookies.json` and re-`auth` with the single token. Treat the old value as leaked to this machine’s disk.

### MCP client (Claude Code / Desktop / Cursor)

```json
{
  "mcpServers": {
    "myfitnesspal": {
      "command": "uv",
      "args": ["run", "--directory", "/ABS/PATH/TO/myfitnesspal-mcp", "mfp-mcp"],
      "env": {
        "MFP_READ_ONLY": "1"
      }
    }
  }
}
```

- No `MFP_COOKIE` in that file.
- `--directory` should be your reviewed clone, not `uvx mfp-mcp` from PyPI, until you pin a version you trust.
- When you later allow writes, remove `MFP_READ_ONLY` **and** accept that the model can change MFP without asking you.

### Least privilege

| Do | Don’t |
| --- | --- |
| stdio, single-user laptop | `--http`, Docker `-p 8484:8484`, cloud agents |
| `MFP_READ_ONLY=1` until you need writes | Give the server to a multi-user or Slack-connected agent |
| Small date queries | `fitness_bulk_export` over years with `sync_first=true` |
| Skip Playwright extra | Persist `browser-profile` in backups |
| Dedicated MFP account | Reuse an account tied to medical/employer programs if that data must not reach an LLM |

### HTTP (only if you ignore the advice)

README already says there is no built-in auth. Minimum that is still not “good”:

- `--host 127.0.0.1` only
- Authenticating reverse proxy or Tailscale **identity-aware** serve, not a raw TCP expose
- Still prefer stdio

### After a suspected leak

1. Unset `MFP_COOKIE` everywhere; delete `cookies.json` and `browser-profile/`.
2. Change the MFP password; sign out other sessions if the product offers it.
3. Rotate any password reused on that account.
4. Re-`auth` only on a machine you trust.

---

## Out of scope / residual risk

**This review did not:**

- Log in to MyFitnessPal or send any request to `myfitnesspal.com` / `api.myfitnesspal.com`
- Exercise Playwright, Cloudflare, or cookie refresh against a live site
- Audit `curl_cffi`, Chromium, or the `mcp` SDK implementations beyond how this repo calls them
- Perform a full dependency CVE campaign (PyPI reports **no** listed vulnerabilities for `myfitnesspal 2.1.2` as of the review date; that is not a guarantee)
- Assess MyFitnessPal’s own session-revocation UX or ToS enforcement

**You still accept, even after the checklist:**

- **LLM/provider exposure** of whatever tools return (food, weight, notes, feel).
- **Unofficial API / WAF cat-and-mouse.** Cloudflare impersonation can fail or get the account flagged. HTML scrape breakage is expected.
- **No official scoped token.** You cannot grant “read diary only” at the MFP layer. `MFP_READ_ONLY` is enforced only inside this process.
- **Local malware / other users on the box** can read `0600` files if they have your UID, or call localhost HTTP, or attach to the MCP host.
- **Upstream `myfitnesspal` + `browser_cookie3` + `cloudscraper`** remain in the environment.
- **Session lifetime ~30 days** (per README). A leaked cookie works until MFP invalidates it.
- **Legal / ToS.** Reverse-engineering private endpoints and evading bot checks may violate MyFitnessPal’s terms. This review is technical, not legal advice.

---

## Hygiene shipped with this PR (behavior notes)

Default tool behavior is unchanged: writes still work unless you set `MFP_READ_ONLY=1`.

| Change | Why it is safe |
| --- | --- |
| `SECURITY_REVIEW.md` (this file) | Documentation only. |
| `MFP_READ_ONLY` | Opt-in; unset keeps current write behavior. |
| Config/data dir `0700`, cookie/DB `0600` | Permissions only. Does not chmod a pre-existing `MFP_MCP_DATA_DIR` whose basename is not `myfitnesspal-mcp` (avoids touching `/tmp` etc.). |
| Non-loopback `--http` warning | stderr only; bind still succeeds. |
| `.gitignore` / README / `.env.example` warnings | Docs and ignore rules. |
