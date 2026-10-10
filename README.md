<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/Mason-Levyy/myfitnesspal-mcp/main/assets/wordmark-dark.png">
  <img alt="myfitnesspal-mcp" src="https://raw.githubusercontent.com/Mason-Levyy/myfitnesspal-mcp/main/assets/wordmark-light.png" width="520">
</picture>

**Log MyFitnessPal by talking to your AI.**

[![PyPI](https://img.shields.io/pypi/v/mfp-mcp?color=0a64e6)](https://pypi.org/project/mfp-mcp/)
[![GitHub stars](https://img.shields.io/github/stars/Mason-Levyy/myfitnesspal-mcp?style=flat&color=0a64e6)](https://github.com/Mason-Levyy/myfitnesspal-mcp/stargazers)
[![CI](https://github.com/Mason-Levyy/myfitnesspal-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Mason-Levyy/myfitnesspal-mcp/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/mfp-mcp)](https://pypi.org/project/mfp-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/Mason-Levyy/myfitnesspal-mcp/blob/main/LICENSE)

[Quickstart](#quickstart) · [Tools](#tools) · [Authentication](#authentication) · [Changelog](https://github.com/Mason-Levyy/myfitnesspal-mcp/blob/main/CHANGELOG.md)

<img alt="Demo: Claude logs each ingredient of a breakfast smoothie with fitness_log_food, and the entries appear in the MyFitnessPal food diary" src="https://raw.githubusercontent.com/Mason-Levyy/myfitnesspal-mcp/main/demo.gif" width="720">

▶ [Watch the launch video](https://github.com/user-attachments/assets/8eeb8516-00ee-42f3-9999-ef77059dddb2)

</div>

Connect MyFitnessPal to Claude or any MCP client. Log meals by talking, search
the food database with macros, track trends, and export your nutrition history, all against your real MyFitnessPal diary.

Published on PyPI as [`mfp-mcp`](https://pypi.org/project/mfp-mcp/).

<!-- mcp-name: io.github.Mason-Levyy/mfp-mcp -->

> **Unofficial.** MyFitnessPal has no public API; this reverse-engineers the
> web app's own endpoints. It can break whenever MFP changes their site. Use at
> your own risk, with your own account.

## Why this one?

MyFitnessPal moved behind Cloudflare + NextAuth, which broke the
username/password login that most existing integrations rely on. This server:

- **Authenticates with your browser session cookie** over a real Chrome TLS
  fingerprint ([curl_cffi](https://github.com/lexiforest/curl_cffi)), which
  passes Cloudflare
- **Auto-refreshes the session** (optional): a headless browser profile rotates
  the token when it expires, and failed calls retry automatically
- **Writes, not just reads**: log, modify, and delete real diary entries
- **Search-then-log**: get candidates with macros, then log the exact item

## Quickstart

1. Connect your account (one-time; prompts you to paste a cookie — see
   [Authentication](#authentication)):

   ```bash
   uvx mfp-mcp auth
   ```

2. Add the server to your client.

   **Claude Code**

   ```bash
   claude mcp add myfitnesspal -- uvx mfp-mcp
   ```

   **Claude Desktop** (`claude_desktop_config.json`)

   ```json
   {
     "mcpServers": {
       "myfitnesspal": {
         "command": "uvx",
         "args": ["mfp-mcp"]
       }
     }
   }
   ```

3. Talk to it: *"log a banana as a snack"*, *"what did I eat yesterday?"*,
   *"chart my weight this month"*.

Requires [uv](https://docs.astral.sh/uv/). Any MCP client that speaks stdio or
streamable HTTP works, not just Claude.

## Authentication

This server signs in with your browser's MyFitnessPal session cookie:

1. Log in at [myfitnesspal.com](https://www.myfitnesspal.com)
2. Open DevTools (F12) → **Application** (Chrome) or **Storage** (Firefox) →
   **Cookies** → `https://www.myfitnesspal.com`
3. Copy the value of `__Secure-next-auth.session-token`
4. Paste it into the `mfp-mcp auth` prompt

On a headless Linux box, skip the prompt. Either command validates the cookie
and writes `cookies.json` with mode `0600`. Unset `MFP_COOKIE` afterwards so
the long-running server reads the file instead of the environment.

```bash
printf '%s\n' "$COOKIE" | mfp-mcp auth
# or, once:
MFP_COOKIE="$COOKIE" mfp-mcp auth
unset MFP_COOKIE
```

Sessions last around 30 days. When one expires, either re-run `auth` or
opt in to auto-refresh. `mfp-mcp auth --check` reports whether the saved
session still works and whether auto-refresh is set up, without prompting or
changing anything.

Paste only `__Secure-next-auth.session-token`. Do not put `MFP_COOKIE` in an
MCP client JSON file. The server is read-only until you set
`MFP_ALLOW_WRITES=1`. To let an assistant log, edit, and delete food and
nothing else, set `MFP_WRITE_TOOLS=food` as well. That alias enables
`fitness_log_food`, `fitness_delete_food`, and `fitness_modify_food`.
`fitness_search_food`, `fitness_find_food`, and the My Foods, saved meals,
recipes, recent, and frequent reads stay available because they are read
tools. Water,
weight, notes, exercise deletes, and the local feel, pin, and draft tools
stay off and are not listed.

```bash
MFP_ALLOW_WRITES=1 MFP_WRITE_TOOLS=food mfp-mcp
```

### Auto-refresh (off unless you opt in)

The `autorefresh` extra can seed a persistent headless browser profile, but
it does nothing until `MFP_AUTOREFRESH=1`. When that is set and MyFitnessPal
rejects the session mid-call, the server tells your client it is retrying,
boots the profile headlessly, lets MyFitnessPal rotate the session token,
saves the fresh cookie, and retries the call. The profile is another copy of
the session cookie on disk.

```bash
uvx --from 'mfp-mcp[autorefresh]' playwright install chromium
MFP_AUTOREFRESH=1 uvx --from 'mfp-mcp[autorefresh]' mfp-mcp auth
```

Then set `MFP_AUTOREFRESH=1` in the client config as well
(`uvx --from 'mfp-mcp[autorefresh]' mfp-mcp`).

## Tools

| Tool | What it does |
| --- | --- |
| `fitness_get_day` | Nutrition totals, diary entries, the MFP daily note, and feel note for a day |
| `fitness_search_food` | Your foods, meals, recipes, recents, and frequents first, then the public database. Each hit has `source` and the ids to log it |
| `fitness_find_food` | Same search as `fitness_search_food` |
| `fitness_list_my_foods` | Foods you created, with calories, macros, servings, and ids |
| `fitness_list_my_meals` | Saved meals and each food in them |
| `fitness_list_my_recipes` | Your recipes. Calories and macros are included when the list page already shows them |
| `fitness_list_recent_foods` | Recent foods from the add-food tabs, including the last serving count. An empty list plus `warnings` means that tab did not respond |
| `fitness_list_frequent_foods` | Frequent foods from the add-food tabs. Same `warnings` behaviour as recent foods |
| `fitness_draft_food` | Numbered options with every serving size, filtered/ranked by optional calorie and macro targets |
| `fitness_log_food` | Log a draft option, a My Food, a recipe, a saved meal, a remembered food, or exact ids |
| `fitness_list_food_pins` | Remembered query → food/serving choices (local) |
| `fitness_clear_food_pin` | Forget one remembered choice, or all of them |
| `fitness_delete_food` | Remove a diary entry by name match |
| `fitness_modify_food` | Replace an entry (or change its quantity), choosing the replacement like `fitness_log_food` |
| `fitness_log_weight` | Log a weight measurement (updates the same day on re-log) |
| `fitness_log_water` | Add water (cups, fl oz, mL, L) or set the day's total with `replace` |
| `fitness_get_exercise` | Read the exercise diary (cardio + strength) |
| `fitness_get_exercise_entries` | List cardio and strength entries with each section's columns |
| `fitness_delete_exercise` | Remove an exercise entry by name match (`all_matches` for multi-row sync cleanup) |
| `fitness_get_note` | Read the MyFitnessPal daily diary note (the "Notes" box) for a day |
| `fitness_log_note` | Write that daily note to MFP (replace, or `append` a new line) |
| `fitness_log_feel` | Save a subjective "how I feel" note (stored locally, never sent to MFP) |
| `fitness_get_trends` | One metric over a date range: weight, calories_in, protein, carbs, fat |
| `fitness_bulk_export` | Whole date range in one call, for analysis |

### Logging flow

Food logging is draft-then-confirm, so what lands in your diary never
depends on MyFitnessPal's search order:

1. `fitness_draft_food("greek yogurt", min_protein=15, max_calories=150)`
   returns numbered options. Each lists every serving size with calories and
   macros for the whole entry (serving × quantity) plus a suggested serving;
   options that meet the targets come first, near misses follow. Call it
   again with different targets to refine.
2. `fitness_log_food(draft_id=..., option=2, serving=1)` logs exactly that
   food and serving, and remembers the choice for that query.
3. Next time, `fitness_log_food(query="greek yogurt")` logs the remembered
   food and serving without searching.

A bare `fitness_log_food(query=...)` checks a remembered choice first, then
your My Foods, saved meals, recipes, recent foods, and frequent foods, and
only then the public database. An exact name, or a single close name, from
those personal sources is logged. A saved-meal match adds every food in the
meal. A recipe match adds one diary line, and `quantity` is how many
servings. If several personal items match, nothing is logged and the draft
includes `personal_matches`. If nothing personal matches, a single exact
public name is logged; otherwise nothing is logged and a draft comes back.
Remembered choices live in the local cache (`fitness_list_food_pins`,
`fitness_clear_food_pin`). Drafts expire after 24 hours.

Log a known personal id without searching:

- `fitness_log_food(my_food_id=..., quantity=2)` logs that My Food.
- `fitness_log_food(recipe_id=..., quantity=2)` logs two servings of the
  recipe as one diary line, through the same recipe logger the website uses.
- `fitness_log_food(saved_meal_id=..., quantity=1)` logs every food in the
  saved meal. `quantity` multiplies each food's own quantity (a tea saved as
  2 servings is logged as 2). `meal` is still the diary section, such as
  breakfast.

`fitness_modify_food` picks the replacement the same way, before deleting
anything: a remembered food, a single personal match, or a single exact
public name is used directly; otherwise the entry is left alone and a draft
comes back — call `fitness_modify_food` again with the same `query` plus
`draft_id` and `option`.

`fitness_log_food`, `fitness_delete_food`, and `fitness_modify_food` all take
a `meal` argument the same way: `breakfast`/`lunch`/`dinner`/`snacks`, or the
literal name of any meal section currently on your diary — including a
renamed default meal or one of the up to two extra meals MyFitnessPal lets
you add in Diary Settings. Matching is case-insensitive against your
account's current meal labels. If you renamed a default meal, the keywords
still work: `breakfast`/`lunch`/`dinner`/`snacks` fall back to your first
through fourth meal, unless that slot now carries a different default name
(e.g. `lunch` won't land in a second meal you renamed to "Dinner"). Any other unrecognized `meal` raises an error instead
of silently logging into the wrong section.

Day summaries and trends read from a local SQLite cache that gap-fills from
MyFitnessPal (first call on a fresh install fetches up to 30 days, one request
per day).

Water logging reads the day's current total from `/food/water`, adds the requested
quantity, and posts the new total back to the same endpoint. Pass
`replace=True` to set the day's total outright (e.g. to fix a mislogged amount);
every call returns `previous_ml`, so a mistake can be reverted by passing it
back as `amount` with `unit="ml"` and `replace=True`. The separate
`/stats` request with a `water_logged` event is analytics telemetry; it does not
persist the water total.

## Remote / HTTP mode

The default transport is stdio. HTTP is off until you pass `--http`, and it
refuses to start unless `MFP_HTTP_TOKEN` is a secret of at least 16 characters.
Clients send `Authorization: Bearer <token>`. The default bind is
`127.0.0.1:8484`, and the Host header must be loopback, which blocks a browser
on another site from reaching the port via DNS rebinding.

```bash
export MFP_HTTP_TOKEN="$(openssl rand -hex 24)"
mfp-mcp --http
```

Binding any other address also requires `MFP_HTTP_ALLOW_LAN=1`. Binding all
interfaces (`0.0.0.0`) additionally requires `MFP_HTTP_ALLOWED_HOSTS` set to
the hostname or IP clients will use. The bearer is sent in cleartext unless
you terminate TLS in front of the process. Do not publish the port to the
internet. Write tools stay off unless `MFP_ALLOW_WRITES=1`.
`MFP_WRITE_TOOLS` can narrow that further (for example `food`).

## Configuration

| Variable | Purpose | Default |
| --- | --- | --- |
| `MFP_COOKIE` | Session cookie (bare token preferred); overrides the saved file. Do not put this in MCP JSON | – |
| `MFP_USERNAME` | Your MFP username (not email); only needed if profile lookup fails | auto-detected |
| `MFP_IMPERSONATE` | curl_cffi browser fingerprint (try `chrome124` on 403s) | `chrome` |
| `MFP_SYNC_DAYS` | Gap-fill lookback window in days | `30` |
| `MFP_MCP_DATA_DIR` | Where the SQLite cache + browser profile live | platform data dir |
| `MFP_ALLOW_WRITES` | Set to `1` to enable write tools. Does nothing while `MFP_READ_ONLY=1` | off |
| `MFP_WRITE_TOOLS` | With `MFP_ALLOW_WRITES=1`, a comma-separated allowlist of write tool names, or `food` for log/edit/delete. Unset means every write tool | all write tools, once writes are on |
| `MFP_READ_ONLY` | Set to `1` to force write tools off | off |
| `MFP_AUTOREFRESH` | Set to `1` to allow headless session refresh | off |
| `MFP_HTTP_TOKEN` | Bearer secret required by `--http` (16+ characters) | – |
| `MFP_HTTP_ALLOW_LAN` | Set to `1` to allow a non-loopback `--host` | off |
| `MFP_HTTP_ALLOWED_HOSTS` | Hostnames or IPs clients send, required for `--host 0.0.0.0` | – |

## Troubleshooting

- **403 / Cloudflare blocked**: try `MFP_IMPERSONATE=chrome124` (or another
  [curl_cffi target](https://github.com/lexiforest/curl_cffi#supported-browsers)).
  Datacenter IPs get challenged far more than residential ones.
- **"Session expired"**: confirm with `mfp-mcp auth --check`, then re-run
  `mfp-mcp auth`, or set `MFP_AUTOREFRESH=1` and use
  [auto-refresh](#auto-refresh-off-unless-you-opt-in).
- **"Write tools are disabled"**: expected unless `MFP_ALLOW_WRITES=1`.
- **"is not enabled"**: `MFP_WRITE_TOOLS` does not name that tool. `food` is log, edit, and delete only.
- **"couldn't read your MyFitnessPal profile"**: MFP's profile endpoint 500s
  for some accounts. Set `MFP_USERNAME` to your username (not your email).
- **curl_cffi install issues**: prebuilt wheels cover Linux/macOS/Windows;
  musl (Alpine) builds from source.

## How it works

- [python-myfitnesspal](https://github.com/coddingtonbear/python-myfitnesspal)
  parses the diary, measurements, and exercise pages — run over a `curl_cffi`
  session that impersonates Chrome's TLS fingerprint so Cloudflare lets it
  through with just the NextAuth session cookie.
- Writes replicate the web app's own XHR calls: the legacy food-search page
  supplies the `food_id`/`weight_id` that `/food/add` accepts, deletes go
  through `/food/remove`, water reads/writes via `/food/water`, and the daily
  note reads/writes via `/food/note` — each with the page CSRF token.
- Day summaries, trends, and exports read a local SQLite cache that gap-fills
  missing days. The MyFitnessPal daily note syncs both ways; feel notes are
  local-only.

## Development

```bash
git clone https://github.com/Mason-Levyy/myfitnesspal-mcp
cd myfitnesspal-mcp
uv sync --extra autorefresh
uv run pytest
```

Tests run against synthetic MyFitnessPal HTML/JSON fixtures — no account
needed. Lint and formatting are enforced with `ruff` (`uv run ruff check .`,
`uv run ruff format .`).

## Contributing

Issues and pull requests are welcome, especially endpoint captures when
MyFitnessPal changes something. See [CONTRIBUTING.md](CONTRIBUTING.md) for
setup, style, and the PR checklist, and [SECURITY.md](SECURITY.md) for how to
report vulnerabilities privately. Release history is in
[CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE)
