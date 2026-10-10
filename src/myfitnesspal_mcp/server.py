import asyncio
import datetime
import functools
import inspect
import os
from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import config, diary, food_logging, mfp_client, personal, refresh, sync
from .food_ranking import MacroTargets
from .store import Store, trend_column

_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

READ_TOOLS = frozenset(
    {
        "fitness_get_day",
        "fitness_search_food",
        "fitness_find_food",
        "fitness_list_my_foods",
        "fitness_list_my_meals",
        "fitness_list_my_recipes",
        "fitness_list_recent_foods",
        "fitness_list_frequent_foods",
        "fitness_list_food_pins",
        "fitness_get_exercise",
        "fitness_get_exercise_entries",
        "fitness_get_note",
        "fitness_get_trends",
        "fitness_bulk_export",
    }
)
WRITE_TOOLS = frozenset(
    {
        "fitness_draft_food",
        "fitness_log_food",
        "fitness_clear_food_pin",
        "fitness_delete_food",
        "fitness_modify_food",
        "fitness_log_weight",
        "fitness_log_water",
        "fitness_delete_exercise",
        "fitness_log_note",
        "fitness_log_feel",
    }
)
# `food` enables diary log/edit/delete, including a saved meal logged through
# fitness_log_food. Search and the personal-library reads stay available
# because they are read tools. Draft, pins, feel, water, weight, notes, and
# exercise stay off.
FOOD_WRITE_TOOLS = frozenset(
    {
        "fitness_log_food",
        "fitness_delete_food",
        "fitness_modify_food",
    }
)


class ReadOnlyError(RuntimeError):
    """A write tool was called while writes are disabled."""


class UnknownWriteToolsError(ValueError):
    """MFP_WRITE_TOOLS names a tool this server does not have."""


def resolve_write_allowlist(raw: str) -> frozenset[str]:
    """Write tools named by MFP_WRITE_TOOLS. `food` is the diary group."""
    enabled: set[str] = set()
    unknown: list[str] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        if token.lower() == "food":
            enabled.update(FOOD_WRITE_TOOLS)
            continue
        if token in WRITE_TOOLS:
            enabled.add(token)
            continue
        if token in READ_TOOLS:
            continue
        unknown.append(token)
    if unknown:
        known = ", ".join(sorted(WRITE_TOOLS))
        raise UnknownWriteToolsError(
            "MFP_WRITE_TOOLS has unknown entries: "
            + ", ".join(unknown)
            + f". Known write tools: {known}. Group alias: food "
            "(fitness_log_food, fitness_delete_food, fitness_modify_food; "
            "fitness_search_food stays available because it is read-only)."
        )
    return frozenset(enabled)


def enabled_write_tools() -> frozenset[str]:
    """Write tools the client may see and call.

    Empty unless MFP_ALLOW_WRITES is set (MFP_READ_ONLY forces that off).
    An unset or blank MFP_WRITE_TOOLS means every write tool. A set value
    is an allowlist.
    """
    if not config.writes_allowed():
        return frozenset()
    raw = os.environ.get("MFP_WRITE_TOOLS")
    if raw is None or not raw.strip():
        return WRITE_TOOLS
    return resolve_write_allowlist(raw)


def assert_writes_allowed(tool_name: str) -> None:
    if not config.writes_allowed():
        raise ReadOnlyError(
            "Write tools are disabled. Set MFP_ALLOW_WRITES=1 to let this "
            "process change MyFitnessPal or local notes. MFP_READ_ONLY=1 "
            "forces them off."
        )
    allowed = enabled_write_tools()
    if tool_name not in allowed:
        listed = ", ".join(sorted(allowed)) or "(none)"
        raise ReadOnlyError(
            f"{tool_name} is not enabled. MFP_WRITE_TOOLS restricts writes to {listed}."
        )


class _GatedFastMCP(FastMCP):
    """Hides write tools the allowlist does not enable."""

    async def list_tools(self):
        allowed = enabled_write_tools()
        tools = await super().list_tools()
        return [
            tool for tool in tools if tool.name in READ_TOOLS or tool.name in allowed
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        allowed = enabled_write_tools()
        if name not in READ_TOOLS and name not in allowed:
            raise ToolError(f"Unknown tool: {name}")
        return await super().call_tool(name, arguments)


mcp = _GatedFastMCP("myfitnesspal")


def write_tool(fn):
    """Register an MCP tool that cannot run unless writes are opted in."""
    tool_name = fn.__name__
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            assert_writes_allowed(tool_name)
            return await fn(*args, **kwargs)

    else:

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            assert_writes_allowed(tool_name)
            return fn(*args, **kwargs)

    wrapper.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
    wrapper.__mfp_write_tool__ = True  # type: ignore[attr-defined]
    return mcp.tool(annotations=_WRITE)(wrapper)


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def parse_day(value: str | None) -> datetime.date:
    if value is None:
        return datetime.date.today()
    return datetime.date.fromisoformat(value)


def parse_range(
    start: str | None, end: str | None, span_days: int = 30
) -> tuple[datetime.date, datetime.date]:
    end_day = parse_day(end)
    if start is None:
        start_day = end_day - datetime.timedelta(days=span_days)
    else:
        start_day = parse_day(start)
    if start_day > end_day:
        raise ValueError("start must be on or before end")
    return start_day, end_day


async def run_with_refresh(ctx: Context, op: Callable[[], Any]) -> Any:
    """Runs a blocking MFP operation; on an auth-shaped failure, notifies the
    client, refreshes the session (headless browser profile when available,
    otherwise re-reads MFP_COOKIE / cookies.json), and retries once."""
    try:
        return await asyncio.to_thread(op)
    except Exception as exc:
        if not mfp_client.is_auth_error(exc):
            raise
        await ctx.info(
            "MyFitnessPal rejected the session — refreshing credentials and retrying."
        )
        try:
            await asyncio.to_thread(refresh.refresh_session)
            result = await asyncio.to_thread(op)
        except Exception as retry_exc:
            await ctx.info("Session refresh failed.")
            raise RuntimeError(
                f"{mfp_client.RECONNECT_HINT} (retry after refresh failed: {retry_exc})"
            ) from retry_exc
        await ctx.info("Session refreshed; the retried call succeeded.")
        return result


async def with_session(ctx: Context, op: Callable[[Store, Any], Any]) -> Any:
    """Runs `op` against the store and a live MFP client, re-resolving both on
    the retry so a refreshed session is picked up."""
    return await run_with_refresh(ctx, lambda: op(get_store(), mfp_client.get_client()))


async def refresh_after_write(ctx: Context, day: datetime.date) -> dict:
    def refresh_op(store, client):
        sync.refresh_day(store, client, day)

    try:
        await with_session(ctx, refresh_op)
    except Exception as exc:
        return {
            "refresh_warning": (
                f"the diary change was saved, but refreshing the local copy of "
                f"{day.isoformat()} failed ({exc}); don't repeat the change"
            )
        }
    return {}


@mcp.tool(annotations=_READ_ONLY)
async def fitness_get_day(date: str | None = None, ctx: Context = None) -> dict:
    """Nutrition summary, diary entries, the MyFitnessPal daily note, and the
    local feel note for a day.

    date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)

    def op(store, client):
        sync.poll(store, client)
        sync.refresh_day(store, client, day)
        return store.day_record(day.isoformat())

    return await with_session(ctx, op)


async def _find_food(query, limit, with_macros, ctx):
    def op(store, client):
        results, warnings = personal.find_food(client, query, limit, with_macros)
        payload = {"query": query, "results": results}
        if warnings:
            payload["warnings"] = warnings
        return payload

    return await with_session(ctx, op)


@mcp.tool(annotations=_READ_ONLY)
async def fitness_search_food(
    query: str, limit: int = 5, with_macros: bool = True, ctx: Context = None
) -> dict:
    """Search the user's foods, meals, recipes, and recents before the public database.

    Personal hits rank first. Each result has name, calories, protein, carbs,
    fat, serving, and source: my_food, my_meal, my_recipe, recent, frequent,
    or public. Ids for logging are included: food_id + version (My Food or
    recent), recipe_id, or meal_id. A recent result's quantity is how many
    servings were last logged. If the recent or frequent tab does not
    respond, it is omitted and named in warnings; the other sources are
    still returned. fitness_find_food returns the same results.
    """
    return await _find_food(query, limit, with_macros, ctx)


@mcp.tool(annotations=_READ_ONLY)
async def fitness_find_food(
    query: str, limit: int = 5, with_macros: bool = True, ctx: Context = None
) -> dict:
    """Same search as fitness_search_food.

    The user's My Foods, saved meals, recipes, recent foods, and frequent
    foods rank ahead of the public database. Every result has a source field.
    A recent or frequent tab that does not respond is named in warnings.
    """
    return await _find_food(query, limit, with_macros, ctx)


@mcp.tool(annotations=_READ_ONLY)
async def fitness_list_my_foods(
    search: str = "", limit: int = 50, ctx: Context = None
) -> dict:
    """List foods the user created (My Foods).

    search: optional name filter. limit: max items (default 50).
    Each item has name, calories, protein, carbs, fat, servings, food_id,
    version, and weight_id. Log one with fitness_log_food(my_food_id=...).
    """

    def op(store, client):
        return {"items": personal.list_my_foods(client, search, limit)}

    return await with_session(ctx, op)


@mcp.tool(annotations=_READ_ONLY)
async def fitness_list_my_meals(
    search: str = "", limit: int = 50, ctx: Context = None
) -> dict:
    """List saved meals (My Meals) and the foods in each one.

    Each meal has meal_id, name, calories, macros, and foods. A food that
    MyFitnessPal returned without ids is still listed by name. Log every
    item with fitness_log_food(saved_meal_id=...).
    """

    def op(store, client):
        return {"items": personal.list_my_meals(client, search, limit)}

    return await with_session(ctx, op)


@mcp.tool(annotations=_READ_ONLY)
async def fitness_list_my_recipes(
    search: str = "", limit: int = 50, ctx: Context = None
) -> dict:
    """List the user's recipes.

    Each recipe has recipe_id and name. Calories, macros, and recipe_servings
    are included when that list page already shows them. Log with
    fitness_log_food(recipe_id=..., quantity=<servings>). quantity is how
    many servings to add, as one diary line, via the recipe logger.
    """

    def op(store, client):
        return {"items": personal.list_my_recipes(client, search, limit)}

    return await with_session(ctx, op)


@mcp.tool(annotations=_READ_ONLY)
async def fitness_list_recent_foods(limit: int = 50, ctx: Context = None) -> dict:
    """Foods from the diary add-food Recent tabs.

    quantity is the last logged serving count. food_id, version, and
    weight_id are enough to log the food. If the tab does not respond,
    items is empty and warnings names the endpoint.
    """

    def op(store, client):
        items, warnings = personal.read_recent_foods(client, limit)
        payload = {"items": items}
        if warnings:
            payload["warnings"] = warnings
        return payload

    return await with_session(ctx, op)


@mcp.tool(annotations=_READ_ONLY)
async def fitness_list_frequent_foods(limit: int = 50, ctx: Context = None) -> dict:
    """Foods from the diary add-food Frequent tabs.

    Each item has name, calories, macros, serving, food_id, version, and
    weight_id. If the tab does not respond, items is empty and warnings
    names the endpoint.
    """

    def op(store, client):
        items, warnings = personal.read_frequent_foods(client, limit)
        payload = {"items": items}
        if warnings:
            payload["warnings"] = warnings
        return payload

    return await with_session(ctx, op)


@write_tool
async def fitness_draft_food(
    query: str,
    quantity: float = 1.0,
    meal: str = "breakfast",
    date: str | None = None,
    min_calories: float | None = None,
    max_calories: float | None = None,
    min_protein: float | None = None,
    max_protein: float | None = None,
    min_carbs: float | None = None,
    max_carbs: float | None = None,
    min_fat: float | None = None,
    max_fat: float | None = None,
    limit: int = 10,
    ctx: Context = None,
) -> dict:
    """Draft a food entry: numbered options to choose from before logging.

    Each option lists every serving size with its calories/protein/carbs/fat
    for the whole entry (serving × quantity) and a suggested_serving. The
    min_/max_ targets (grams; calories in kcal) also apply to the whole
    entry: options that fit come first, near misses follow, flagged
    fits_targets=false. A food you've confirmed before for this query is
    pinned to the top. The order is fixed once drafted.

    Log a choice with fitness_log_food(draft_id=..., option=N, serving=M);
    call this again with different targets to refine. Drafts last 24 hours.
    date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)
    targets = MacroTargets(
        min_calories=min_calories,
        max_calories=max_calories,
        min_protein=min_protein,
        max_protein=max_protein,
        min_carbs=min_carbs,
        max_carbs=max_carbs,
        min_fat=min_fat,
        max_fat=max_fat,
    )

    def op(store, client):
        return food_logging.draft_food(
            client, store, query, day, meal, quantity, targets, limit
        )

    return await with_session(ctx, op)


@write_tool
async def fitness_log_food(
    query: str | None = None,
    meal: str | None = None,
    quantity: float | None = None,
    date: str | None = None,
    draft_id: str | None = None,
    option: int | None = None,
    serving: int | None = None,
    pin: bool = True,
    my_food_id: str | None = None,
    recipe_id: str | None = None,
    saved_meal_id: str | None = None,
    food_id: str | None = None,
    weight_id: str | None = None,
    ctx: Context = None,
) -> dict:
    """Log a food, recipe, or saved meal to the real MyFitnessPal diary.

    Preferred: pick from a fitness_draft_food draft with draft_id + option
    (+ serving, default the option's suggested_serving). meal, quantity, and
    date default to the draft's. pin=True remembers the choice so logging
    the same query later reuses this food and serving.

    my_food_id logs that My Food (first serving size). recipe_id logs the
    recipe as one diary line through the recipe logger; quantity is the
    number of servings.
    saved_meal_id logs every food in the saved meal. Each component's own
    quantity is multiplied by quantity. meal is the diary section
    (breakfast, lunch, dinner, snacks, or a custom meal name), not the
    saved meal.

    With only `query`: a remembered pin wins. Otherwise an exact personal
    name, or a single close personal name, is logged (a meal match logs the
    whole meal; a recipe match logs that many servings). Several personal
    matches log nothing and return a draft plus personal_matches. If nothing
    personal matches, a single exact public name is logged; otherwise nothing
    is logged and a draft is returned (needs_choice=true). A recent or
    frequent tab that does not respond is skipped and named in warnings.

    food_id + weight_id (a public search hit) logs that item. `query` is its
    display name.

    meal: breakfast|lunch|dinner|snacks (default breakfast) or any meal name
    on the account. quantity: number of servings (default 1).
    date: YYYY-MM-DD (default today).
    """
    explicit_day = parse_day(date) if date else None
    day = explicit_day or parse_day(None)
    chosen_meal = meal or "breakfast"
    chosen_quantity = 1.0 if quantity is None else quantity

    def op(store, client):
        if draft_id is not None:
            if option is None:
                raise ValueError("option is required with draft_id")
            result = food_logging.log_from_draft(
                client,
                store,
                draft_id,
                option,
                serving=serving,
                quantity=quantity,
                meal=meal,
                day=explicit_day,
                pin=pin,
            )
        elif my_food_id:
            result = personal.log_my_food(
                client, my_food_id, day, chosen_meal, chosen_quantity
            )
        elif recipe_id:
            result = personal.log_recipe(
                client, recipe_id, day, chosen_meal, chosen_quantity
            )
        elif saved_meal_id:
            result = personal.log_saved_meal(
                client, saved_meal_id, day, chosen_meal, chosen_quantity
            )
        elif food_id is not None and weight_id is not None:
            food = {
                "food_id": food_id,
                "weight_id": weight_id,
                "name": query or food_id,
            }
            logged = food_logging.log_exact(
                client,
                food,
                day,
                chosen_meal,
                chosen_quantity,
            )
            result = {**logged, "source": "ids"}
        elif query:
            result = food_logging.log_by_query(
                client,
                store,
                query,
                day,
                chosen_meal,
                chosen_quantity,
            )
        else:
            raise ValueError(
                "pass draft_id + option, my_food_id, recipe_id, saved_meal_id, "
                "food_id + weight_id, or query"
            )
        return result

    result = await with_session(ctx, op)
    if not result.get("logged"):
        return {"ok": True, **result}
    refresh_warning = await refresh_after_write(
        ctx, datetime.date.fromisoformat(result["date"])
    )
    return {
        "ok": True,
        **result,
        **refresh_warning,
        "day": get_store().day_record(result["date"]),
    }


@mcp.tool(annotations=_READ_ONLY)
def fitness_list_food_pins() -> dict:
    """List remembered food choices (query → food and serving) that
    fitness_log_food reuses. Stored locally only."""
    return {"pins": get_store().pins()}


@write_tool
def fitness_clear_food_pin(query: str | None = None, clear_all: bool = False) -> dict:
    """Forget a remembered food choice for `query`, or every choice with
    clear_all=True, so the next log of that query drafts options again."""
    store = get_store()
    if clear_all:
        return {"cleared": store.clear_pins()}
    if not query:
        raise ValueError("pass query, or clear_all=True")
    return {"cleared": int(store.clear_pin(query))}


@write_tool
async def fitness_delete_food(
    query: str, meal: str | None = None, date: str | None = None, ctx: Context = None
) -> dict:
    """Remove a food from the MyFitnessPal diary by name match.

    query: text matched against logged entry names (e.g. "banana"). If this
    matches nothing, the error lists what's actually logged that day/meal — use
    that to retry with a better query. If it matches more than one entry (and
    none is an exact name match), the error lists the candidates; narrow `query`
    to pick one.
    meal: optional breakfast|lunch|dinner|snacks or any meal name on the
    account, to disambiguate duplicates. date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)

    def op(store, client):
        return diary.delete_food(client, day, query, meal)

    result = await with_session(ctx, op)
    return {"ok": True, **result, **await refresh_after_write(ctx, day)}


@write_tool
async def fitness_modify_food(
    query: str,
    new_query: str | None = None,
    meal: str = "breakfast",
    quantity: float | None = None,
    date: str | None = None,
    draft_id: str | None = None,
    option: int | None = None,
    serving: int | None = None,
    ctx: Context = None,
) -> dict:
    """Replace a MyFitnessPal diary entry: deletes the match, then adds a food.

    query: the existing entry to replace (name match) within `meal`. If this
    matches nothing, the error lists what's actually logged that day/meal — use
    that to retry with a better query. If it matches more than one entry (and
    none is an exact name match), the error lists the candidates; narrow `query`
    to pick one.
    new_query: the food to add instead; omit to re-add `query` (e.g. to change
    quantity). The replacement is chosen like fitness_log_food's query path:
    a pin, then an exact or single close match from My Foods, saved meals,
    recipes, recents, and frequents, then a single exact public name.
    Otherwise nothing is changed and a draft comes back (needs_choice=true)
    — call again with the same query plus draft_id + option (and optionally
    serving).
    meal: breakfast|lunch|dinner|snacks or any meal name on the account,
    used for both the delete and the add. quantity: servings (default 1, or
    the draft's). date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)

    def op(store, client):
        return food_logging.modify_food(
            client,
            store,
            day,
            meal,
            query,
            new_query,
            quantity,
            draft_id=draft_id,
            option=option,
            serving=serving,
        )

    result = await with_session(ctx, op)
    if result.get("needs_choice"):
        return {"ok": True, **result}
    return {"ok": True, **result, **await refresh_after_write(ctx, day)}


@write_tool
async def fitness_log_weight(
    weight: float, date: str | None = None, ctx: Context = None
) -> dict:
    """Log a weight measurement to MyFitnessPal.

    weight: in your MyFitnessPal account's display unit (kg or lbs).
    Logging twice for the same date updates that day's measurement.
    date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)

    def op(store, client):
        result = diary.set_weight(client, day, weight)
        store.upsert_nutrition(day.isoformat(), weight=result["weight"])
        return {"ok": True, **result}

    return await with_session(ctx, op)


@write_tool
async def fitness_log_water(
    amount: float,
    unit: str = "cup",
    replace: bool = False,
    date: str | None = None,
    ctx: Context = None,
) -> dict:
    """Log water to the real MyFitnessPal water tracker.

    amount: quantity to add to the day's total. unit: cup | fl_oz | ml | l
    (common spellings like "cups", "oz", "fl oz", "milliliters", "litres" are
    accepted). replace: set the day's total to `amount` instead of adding —
    use it to correct a mislogged total (amount=0 clears the day).
    Returns previous_ml; to undo a mistake, call again with
    amount=previous_ml, unit="ml", replace=True.
    date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)

    def op(store, client):
        result = diary.log_water(client, day, amount, unit, replace=replace)
        store.upsert_nutrition(day.isoformat(), water_ml=result["water_ml"])
        return {"ok": True, **result}

    return await with_session(ctx, op)


@mcp.tool(annotations=_READ_ONLY)
async def fitness_get_exercise(date: str | None = None, ctx: Context = None) -> dict:
    """Read the MyFitnessPal exercise diary (cardio + strength) for a day.

    date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)

    def op(store, client):
        return diary.get_exercise(client, day)

    return await with_session(ctx, op)


@mcp.tool(annotations=_READ_ONLY)
async def fitness_get_exercise_entries(
    date: str | None = None, ctx: Context = None
) -> dict:
    """List a day's cardio and strength exercise-diary entries.

    Each entry has its section, name, and that section's columns (cardio:
    minutes, calories_burned; strength: sets, reps_set, weight_set). entry_id
    is informational only — fitness_delete_exercise deletes by name match.
    date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)

    def op(store, client):
        doc, _ = diary.exercise_page(client, day)
        return {"day": day.isoformat(), "entries": diary.exercise_entries(doc)}

    return await with_session(ctx, op)


@write_tool
async def fitness_delete_exercise(
    query: str,
    date: str | None = None,
    all_matches: bool = False,
    ctx: Context = None,
) -> dict:
    """Remove exercise-diary entries by name match (case-insensitive).

    query: text matched against logged exercise names. By default deletes
    exactly one entry, like fitness_delete_food: an exact name match wins;
    otherwise several matches raise an error listing the candidates, and no
    match raises an error listing what's actually logged.
    all_matches: delete every entry whose name contains `query` — for
    cleaning up syncs that log one workout as several same-named rows
    (e.g. Garmin's "Aerobics, general"). Check fitness_get_exercise_entries
    first; a short query can match many entries.
    date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)

    def op(store, client):
        return diary.delete_exercise(client, day, query, all_matches=all_matches)

    return await with_session(ctx, op)


@mcp.tool(annotations=_READ_ONLY)
async def fitness_get_note(date: str | None = None, ctx: Context = None) -> dict:
    """Read the MyFitnessPal daily diary note (the free-text 'Notes' box at the
    bottom of the day) straight from your account.

    date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)

    def op(store, client):
        body = diary.get_note(client, day)
        store.set_note(day.isoformat(), body)
        return {"day": day.isoformat(), "note": body}

    return await with_session(ctx, op)


@write_tool
async def fitness_log_note(
    text: str, date: str | None = None, append: bool = False, ctx: Context = None
) -> dict:
    """Write the MyFitnessPal daily diary note (the free-text 'Notes' box at the
    bottom of the day). This is your real MFP note, synced to your account —
    distinct from the local-only fitness_log_feel.

    text: the note body. append: add to the existing note on a new line instead
    of replacing it. date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)

    def op(store, client):
        result = diary.push_note(client, day, text, append=append)
        store.set_note(day.isoformat(), result["note"])
        return {"ok": True, **result}

    return await with_session(ctx, op)


@write_tool
def fitness_log_feel(
    note: str | None = None, rating: int | None = None, date: str | None = None
) -> dict:
    """Save a 'how I feel today' note. Stored locally only — never sent to
    MyFitnessPal.

    rating: optional 1-5. date: YYYY-MM-DD (default: today).
    """
    day = parse_day(date)
    return get_store().set_feel(day.isoformat(), note, rating)


@mcp.tool(annotations=_READ_ONLY)
async def fitness_get_trends(
    metric: str, start: str | None = None, end: str | None = None, ctx: Context = None
) -> dict:
    """A single metric over a date range, for charts/analysis.

    metric: weight | calories_in | protein | carbs | fat.
    start/end: YYYY-MM-DD (default: last 30 days).
    Returns {metric, points: [{day, value}, ...]} with nulls omitted.
    """
    trend_column(metric)
    start_day, end_day = parse_range(start, end)

    def op(store, client):
        sync.poll(store, client)
        return {
            "metric": metric,
            "points": store.trend(metric, start_day.isoformat(), end_day.isoformat()),
        }

    return await with_session(ctx, op)


@mcp.tool(annotations=_READ_ONLY)
async def fitness_bulk_export(
    start: str | None = None,
    end: str | None = None,
    sync_first: bool = False,
    ctx: Context = None,
) -> dict:
    """Export a whole date range at once for analysis: per-day nutrition
    totals, food entries with macros, the MyFitnessPal daily note, and local
    feel notes. Read-only.

    start/end: YYYY-MM-DD (default: last 30 days ending today).
    sync_first: gap-fill from MyFitnessPal before exporting. Off by default so
    large historical exports stay fast on cached data.
    """
    start_day, end_day = parse_range(start, end)

    def op():
        store = get_store()
        if sync_first:
            span = (end_day - start_day).days + 1
            sync.poll(store, mfp_client.get_client(), days=span, force=True)
        days = store.export_range(start_day.isoformat(), end_day.isoformat())
        return {
            "start": start_day.isoformat(),
            "end": end_day.isoformat(),
            "count": len(days),
            "days": days,
        }

    return await run_with_refresh(ctx, op)
