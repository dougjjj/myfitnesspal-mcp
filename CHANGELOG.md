# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `fitness_list_my_foods`, `fitness_list_my_meals`, `fitness_list_my_recipes`,
  `fitness_list_recent_foods`, and `fitness_list_frequent_foods` read the
  user's MyFitnessPal library. Each item includes name, calories, macros,
  servings, and the ids needed to log it.
- `fitness_find_food` searches that library before the public database.
- `fitness_log_food` accepts `my_food_id`, `recipe_id` (quantity is servings
  of the recipe, one diary line), and `saved_meal_id` (logs every food in
  the meal). Saved-meal logging stays in the `food` write group.

### Changed

- `fitness_search_food` checks My Foods, saved meals, recipes, recent foods,
  and frequent foods before the public database. Each result has `source`
  (`my_food`, `my_meal`, `my_recipe`, `recent`, `frequent`, or `public`).
  Personal hits rank first. `fitness_find_food` returns the same results.
- `fitness_log_food(query=...)` prefers a remembered pin, then an exact or
  unique close match from those personal sources (a meal logs every item; a
  recipe logs that many servings). Several personal matches return a draft
  with `personal_matches` and log nothing. A single exact public name is
  used only when nothing personal matches.
- Search, find, the recent and frequent lists, and a query log or edit that
  had to read those tabs may include `warnings` when a tab does not respond.
  Callers that ignore unknown fields keep working.

### Fixed

- `fitness_log_food(recipe_id=...)` submits the recipe through MyFitnessPal's
  recipe logger (`POST /recipe/log_recipe`) using the recipe object on the
  view page. A recipe with no food id can be logged. `quantity` is still how
  many servings to add, and the diary still gets one line.
- Recent and frequent food lookups give up after 8 seconds.
  `fitness_find_food`, `fitness_search_food`, `fitness_list_recent_foods`,
  and `fitness_list_frequent_foods` return the other results with a
  `warnings` field naming the endpoint that did not respond.
- A recipe list includes calories, macros, and `recipe_servings` when that
  list page already includes them. Listing recipes does not open each one.

### Security

- Write tools are off unless `MFP_ALLOW_WRITES=1`. `MFP_READ_ONLY=1` forces
  them off. This covers food, water, weight, notes, exercise deletes, and the
  local feel-note, pin, and draft tools.
- `MFP_WRITE_TOOLS` narrows that opt-in. `food` enables only
  `fitness_log_food`, `fitness_delete_food`, and `fitness_modify_food`.
  Other write tools are not listed to the client. Unset, every write tool
  stays available once `MFP_ALLOW_WRITES=1`.
- `mfp-mcp auth` can take the session cookie from `MFP_COOKIE` or from stdin
  on a headless machine and writes `cookies.json` with mode `0600`.
- `--http` still defaults to `127.0.0.1` and now refuses to start without
  `MFP_HTTP_TOKEN`. Binding any other address also requires
  `MFP_HTTP_ALLOW_LAN=1` and a Host allowlist (`MFP_HTTP_ALLOWED_HOSTS` when
  binding all interfaces).
- Headless auto-refresh stays off unless `MFP_AUTOREFRESH=1`, even when the
  `[autorefresh]` extra is installed.
- New config and data directories are mode `0700`. `cookies.json` is created
  mode `0600`. The SQLite file is mode `0600`.

## [0.4.1] - 2026-09-30

### Added

- `auth --check` reports where the session comes from, whether MyFitnessPal
  still accepts it, and whether auto-refresh is ready. It never prompts or
  saves, and exits 1 when the session needs attention.

### Fixed

- When several tool calls hit an expired session at once, only one
  headless-browser refresh runs and the others reuse it. Previously each call
  launched Chromium against the same profile, and every launch after the
  first failed.
- Food diary reads and writes, and cache syncs, use `/food/diary?date=`
  instead of `/food/diary/{username}?date=`. Since 2026-09-30 MyFitnessPal
  answers the username form with its marketing homepage, which made every
  diary write fail as signed out and let syncs cache days as empty (#24).
- Parallel tool calls on the first request of the day fetch the sync window
  from MyFitnessPal once instead of once per call, and no longer read a day's
  diary while a sync is rewriting it.

## [0.4.0] - 2026-09-28

### Added

- `fitness_get_exercise_entries` lists a day's cardio and strength entries
  with each section's own columns; `fitness_delete_exercise` removes one
  entry by name match, or every match with `all_matches=True` (for syncs that
  split one workout into several same-named rows).
- `fitness_log_water`: add water to MyFitnessPal's water tracker in cups,
  fluid ounces, millilitres, or litres (common spellings accepted), or set the
  day's total with `replace=True`.
- `fitness_draft_food`: numbered food options with every serving size and
  whole-entry macros, ranked deterministically and filtered by optional
  min/max calorie, protein, carb, and fat targets; stored as a 24-hour draft.
- `fitness_log_food` accepts `draft_id` + `option` (+ `serving`) and
  remembers the choice per query; `fitness_list_food_pins` and
  `fitness_clear_food_pin` manage remembered choices.
- Contributor documentation: `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`,
  `SECURITY.md`, issue and pull request templates, `CODEOWNERS`.
- Ruff lint and format checks in CI, with a `pre-commit` config.
- Dependabot for GitHub Actions and Python dependencies.
- README wordmark, a link to the launch video, and a GitHub stars badge.
  The wordmark lives in `assets/`, which is left out of the sdist.

### Changed

- `fitness_log_food(query=...)` no longer logs the top search result. It
  logs a remembered food, or a single exact-name match; otherwise it logs
  nothing and returns a draft to choose from. A single exact match is read
  off the search page, with no per-result detail requests.
- `fitness_modify_food` chooses its replacement the same way (remembered
  food, single exact match, or a draft) before deleting anything; an
  ambiguous replacement changes nothing and is confirmed with `draft_id` +
  `option`. Its result reports `logged` (plus `food_id`, `weight_id`,
  `serving`) in place of `added`.
- `fitness_log_food` with `food_id` + `weight_id` now also returns `serving`.

### Fixed

- A session that lapsed while re-syncing the day after `fitness_log_food`,
  `fitness_delete_food` or `fitness_modify_food` retried the whole call, so
  the food was logged (or removed) twice. Only the re-sync is retried now; if
  it still fails, the result carries `refresh_warning` instead of an error.
- Drafts no longer fail outright when one search result has a malformed
  serving size; that serving is skipped.
- Confirming a pinned food whose serving list failed to load no longer
  replaces the pin with the default serving.
- Foods whose serving sizes can't be loaded are ranked on their search
  listing's calories instead of as a full miss against macro targets.

- Gap-fill now keys off an explicit `diary_synced` flag instead of row
  existence. Previously a weight-only row (from `fitness_log_weight` on a past
  date, or from the weigh-in backfill after a failed day fetch) made sync treat
  that day as cached, so its calories and macros were never fetched. Existing
  databases are migrated on first open; weight-only rows are refetched on the
  next sync.
- `fitness_log_food`'s `meal` argument now resolves against the account's
  actual current meal labels (scraped off the diary page), the same way
  `fitness_delete_food`/`fitness_modify_food` already matched entries. It
  previously only recognized the literal keywords `breakfast`/`lunch`/
  `dinner`/`snacks` via a hardcoded 0-3 index and silently fell back to
  meal_id 0 for anything else — misfiling entries for accounts with renamed
  meals or the up to two extra custom meals MyFitnessPal allows. The four
  keywords still reach the first four meals when those were renamed (but
  never a slot now named after a different default meal); any other
  unresolvable `meal` now raises instead of defaulting to meal 0. Matching
  ignores extra and non-breaking spaces, and an unnamed meal section keeps
  its position. A diary page with no meal sections (a lapsed session) now
  triggers the session refresh instead of an unknown-meal error.

## [0.3.0] - 2026-07-26

### Changed

- Diary entry matching for `fitness_delete_food` and `fitness_modify_food`
  disambiguates instead of guessing when several entries match.
- Releases are tagged and published automatically when the version in
  `pyproject.toml` is bumped on `main`.

### Fixed

- `__version__` no longer drifts from `pyproject.toml`.

## [0.2.1] - 2026-07-25

### Fixed

- Weight backfill no longer strands weigh-ins on days that were already cached.

## [0.2.0] - 2026-07-09

### Added

- `fitness_get_note` and `fitness_log_note` for reading and writing the
  MyFitnessPal daily diary note.
- Demo GIF in the README.

## [0.1.3] - 2026-07-08

### Fixed

- MCP registry namespace case (`io.github.Mason-Levyy`).

## [0.1.2] - 2026-07-08

### Added

- Publishing to the MCP Registry (`server.json`, OIDC login).

## [0.1.1] - 2026-07-08

### Added

- `build_client` accepts username and impersonation overrides so the client
  can be embedded in other projects.

## [0.1.0] - 2026-07-08

### Added

- Initial release: MCP server with diary read/write, food search, weight and
  exercise logging, trends, bulk export, cookie auth with optional headless
  auto-refresh, stdio and streamable HTTP transports. Published to PyPI as
  `mfp-mcp`.

[Unreleased]: https://github.com/Mason-Levyy/myfitnesspal-mcp/compare/v0.4.1...HEAD
[0.4.1]: https://github.com/Mason-Levyy/myfitnesspal-mcp/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/Mason-Levyy/myfitnesspal-mcp/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Mason-Levyy/myfitnesspal-mcp/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/Mason-Levyy/myfitnesspal-mcp/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Mason-Levyy/myfitnesspal-mcp/compare/v0.1.3...v0.2.0
[0.1.3]: https://github.com/Mason-Levyy/myfitnesspal-mcp/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/Mason-Levyy/myfitnesspal-mcp/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/Mason-Levyy/myfitnesspal-mcp/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Mason-Levyy/myfitnesspal-mcp/releases/tag/v0.1.0
