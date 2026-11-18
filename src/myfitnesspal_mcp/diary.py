"""Diary reads and writes against MyFitnessPal's web endpoints.

MFP has no official write API. These calls replicate what the web app's own
JavaScript sends: the legacy /food/search page exposes per-result food_id +
weight_id (the ids /food/add accepts — v2 API ids are rejected), and writes
authenticate with the session's Bearer token plus mfp-* client headers and the
page csrf token.
"""

from datetime import date
from html import unescape
from urllib import parse

from lxml import html as lh

MEAL_ALIASES = {"snack": "snacks"}

DEFAULT_MEAL_POSITIONS = {"breakfast": 0, "lunch": 1, "dinner": 2, "snacks": 3}


def _collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


def _normalize_meal(meal: str | None) -> str | None:
    if meal is None:
        return None
    lowered = _collapse_whitespace(meal).lower()
    return MEAL_ALIASES.get(lowered, lowered)


class DiaryLookupError(RuntimeError):
    pass


class UnknownMeal(DiaryLookupError):
    pass


class DiarySignedOut(RuntimeError):
    pass


def _is_meal_header(row) -> bool:
    return "meal_header" in (row.get("class") or "")


def _header_label(header_row, position: int) -> str:
    cells = header_row.xpath("./td")
    name = _collapse_whitespace(cells[0].text_content()) if cells else ""
    return name or f"Meal {position + 1}"


def meal_headers(doc) -> list[str]:
    header_rows = [row for row in doc.xpath("//tr") if _is_meal_header(row)]
    return [_header_label(row, position) for position, row in enumerate(header_rows)]


def resolve_meal(doc, meal: str) -> tuple[str, str]:
    headers = meal_headers(doc)
    target = _normalize_meal(meal)
    for position, header in enumerate(headers):
        if _normalize_meal(header) == target:
            return str(position), header
    default_position = DEFAULT_MEAL_POSITIONS.get(target)
    if default_position is not None and default_position < len(headers):
        header_at_default = headers[default_position]
        if _normalize_meal(header_at_default) not in DEFAULT_MEAL_POSITIONS:
            return str(default_position), header_at_default
    raise UnknownMeal(
        f"no MyFitnessPal meal named {meal!r}. Configured meals: " + ", ".join(headers)
    )


def api_headers(client, extra: dict | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {client.access_token}",
        "mfp-client-id": "mfp-main-js",
        "mfp-user-id": str(client.user_id),
        "X-Requested-With": "XMLHttpRequest",
    }
    if extra:
        headers.update(extra)
    return headers


def _result_extras(anchor) -> dict:
    extras = {
        "external_id": anchor.get("data-external-id"),
        "brand": None,
        "serving": None,
        "calories": None,
    }
    containers = anchor.xpath("ancestor::li[1]")
    if not containers:
        return extras
    info = containers[0].xpath(".//p[@class='search-nutritional-info']")
    if not info or not info[0].text:
        return extras
    parts = info[0].text.strip().split(",")
    if len(parts) >= 3:
        extras["brand"] = " ".join(parts[0:-2]).strip()
    if len(parts) >= 2:
        extras["serving"] = parts[-2].strip() or None
    calories_text = parts[-1].replace("calories", "").strip()
    try:
        extras["calories"] = float(calories_text)
    except ValueError:
        pass
    return extras


def food_search(client, query: str):
    """Returns (results, csrf_token) scraped from the legacy web search page.

    Each result: {food_id, weight_id, name, external_id, brand, calories} —
    food_id/weight_id feed /food/add; external_id feeds the v2 details API.
    """
    url = parse.urljoin(
        client.BASE_URL_SECURE, f"food/search?search={parse.quote(query)}&page=1"
    )
    resp = client.session.get(url, headers=api_headers(client))
    resp.raise_for_status()
    doc = lh.fromstring(resp.text)
    csrf = doc.xpath("//meta[@name='csrf-token']/@content")
    results = []
    for anchor in doc.xpath("//a[@data-original-id and @data-weight-ids]"):
        weight_ids = [w for w in anchor.get("data-weight-ids").split(",") if w]
        if not weight_ids:
            continue
        result = {
            "food_id": anchor.get("data-original-id"),
            "weight_id": weight_ids[0],
            "weight_ids": weight_ids,
            "name": anchor.text_content().strip(),
        }
        result.update(_result_extras(anchor))
        results.append(result)
    if csrf:
        return results, csrf[0]
    return results, None


def serving_label(serving: dict) -> str | None:
    parts = []
    value = serving.get("value")
    if isinstance(value, (int, float)):
        parts.append(f"{value:g}")
    elif value is not None:
        parts.append(str(value))
    if serving.get("unit"):
        parts.append(str(serving["unit"]))
    return " ".join(parts) or None


def _nutrition_multiplier(serving: dict) -> float | None:
    try:
        return float(serving["nutrition_multiplier"])
    except (KeyError, TypeError, ValueError):
        return None


def pair_servings(weight_ids: list[str], serving_sizes: list[dict]) -> list[dict]:
    if len(weight_ids) != len(serving_sizes):
        return []
    servings = []
    for weight_id, serving in zip(weight_ids, serving_sizes, strict=True):
        if not isinstance(serving, dict):
            continue
        multiplier = _nutrition_multiplier(serving)
        label = serving_label(serving)
        if multiplier is None or label is None:
            continue
        servings.append(
            {"weight_id": weight_id, "label": label, "nutrition_multiplier": multiplier}
        )
    return servings


def food_details(client, external_id: str | None) -> dict | None:
    if not external_id:
        return None
    try:
        details = client._get_food_item_details(int(external_id))
        nutrition = details["nutrition"]
        return {
            "verified": details.get("verified"),
            "nutrition": {
                "calories": details["calories"],
                "protein": nutrition.get("protein"),
                "carbs": nutrition.get("carbohydrates"),
                "fat": nutrition.get("fat"),
            },
            "serving_sizes": details.get("serving_sizes") or [],
        }
    except Exception:
        return None


def food_candidates(
    client, query: str, limit: int = 10, search_results: list[dict] | None = None
) -> list[dict]:
    if search_results is None:
        search_results, _ = food_search(client, query)
    candidates = []
    for search_rank, result in enumerate(search_results[:limit]):
        candidate = {
            "food_id": result["food_id"],
            "name": result["name"],
            "brand": result["brand"],
            "verified": None,
            "search_rank": search_rank,
            "nutrition": {"calories": result["calories"]},
            "servings": [],
            "default_weight_id": result["weight_id"],
        }
        details = food_details(client, result["external_id"])
        if details:
            candidate["verified"] = details["verified"]
            candidate["nutrition"] = details["nutrition"]
            candidate["servings"] = pair_servings(
                result["weight_ids"], details["serving_sizes"]
            )
        if not candidate["servings"]:
            candidate["nutrition"] = {"calories": result["calories"]}
            candidate["servings"] = [
                {
                    "weight_id": result["weight_id"],
                    "label": result["serving"] or "default serving",
                    "nutrition_multiplier": 1.0,
                }
            ]
        candidates.append(candidate)
    return candidates


def search_food(
    client, query: str, limit: int = 5, with_macros: bool = True
) -> list[dict]:
    results, _ = food_search(client, query)
    candidates = []
    for result in results[:limit]:
        candidate = {
            "name": result["name"],
            "brand": result["brand"],
            "calories": result["calories"],
            "protein": None,
            "carbs": None,
            "fat": None,
            "serving": None,
            "verified": None,
            "food_id": result["food_id"],
            "weight_id": result["weight_id"],
        }
        details = food_details(client, result["external_id"]) if with_macros else None
        if details:
            candidate.update(details["nutrition"])
            candidate["verified"] = details["verified"]
            if details["serving_sizes"]:
                candidate["serving"] = serving_label(details["serving_sizes"][0])
        candidates.append(candidate)
    return candidates


def add_food_to_diary(
    client, food_id, weight_id, csrf, meal_id: str, day: date, quantity
):
    resp = client.session.post(
        parse.urljoin(client.BASE_URL_SECURE, "food/add"),
        data={
            "food_entry[food_id]": str(food_id),
            "food_entry[date]": day.isoformat(),
            "food_entry[quantity]": str(quantity),
            "food_entry[weight_id]": str(weight_id),
            "food_entry[meal_id]": meal_id,
            "ajax": "true",
        },
        headers=api_headers(
            client,
            {
                "Accept": "application/json",
                "X-CSRF-Token": csrf,
                "Origin": "https://www.myfitnesspal.com",
                "Referer": parse.urljoin(client.BASE_URL_SECURE, "food/search"),
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        ),
    )
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"MyFitnessPal /food/add returned HTTP {resp.status_code}")


def push_food(
    client,
    day: date,
    meal: str,
    food_id: str,
    weight_id: str,
    quantity: float = 1.0,
    page: tuple | None = None,
) -> None:
    doc, csrf = page or diary_page(client, day)
    meal_id, _ = resolve_meal(doc, meal)
    add_food_to_diary(client, food_id, weight_id, csrf, meal_id, day, quantity)


def food_diary_url(client, day: date | None = None) -> str:
    url = parse.urljoin(client.BASE_URL_SECURE, "food/diary")
    if day is None:
        return url
    return f"{url}?date={day.isoformat()}"


def diary_page(client, day: date):
    resp = client.session.get(food_diary_url(client, day), headers=api_headers(client))
    resp.raise_for_status()
    doc = lh.fromstring(resp.text)
    tokens = doc.xpath("//meta[@name='csrf-token']/@content")
    if not tokens:
        raise RuntimeError("couldn't read the MyFitnessPal csrf token")
    if not meal_headers(doc):
        raise DiarySignedOut(
            "the MyFitnessPal diary page has no meal sections; the login "
            "session has likely expired"
        )
    return doc, tokens[0]


def diary_entries(doc) -> list[dict]:
    entries = []
    current_meal = None
    current_label = None
    meal_position = -1
    position_in_meal = 0
    for row in doc.xpath("//tr"):
        if _is_meal_header(row):
            meal_position += 1
            current_label = _header_label(row, meal_position)
            current_meal = _normalize_meal(current_label)
            position_in_meal = 0
            continue
        anchors = row.xpath(".//a[@data-food-entry-id]")
        if anchors:
            position_in_meal += 1
            entries.append(
                {
                    "entry_id": anchors[0].get("data-food-entry-id"),
                    "meal": current_meal,
                    "meal_label": current_label,
                    "name": anchors[0].text_content().strip(),
                    "index": position_in_meal,
                }
            )
    return entries


def _format_entry(entry: dict) -> str:
    details = []
    if entry.get("meal"):
        details.append(f"meal={entry['meal']}")
    entry_id = entry.get("entry_id")
    if entry_id:
        details.append(f"entry_id={entry_id}")
    if entry.get("index") is not None:
        details.append(f"index={entry['index']}")
    label = f"{entry['name']!r}"
    if not details:
        return label
    return f"{label} ({', '.join(details)})"


def _entry_name_key(name: str | None) -> str:
    return _collapse_whitespace(name or "").lower()


def _with_identity(row: dict, parsed_row: dict | None) -> dict:
    if parsed_row is None:
        return {**row, "entry_id": None, "index": None}
    return {
        **row,
        "entry_id": parsed_row.get("entry_id"),
        "index": parsed_row.get("index"),
    }


def _identity_only(parsed_row: dict) -> dict:
    return {
        "meal": parsed_row.get("meal_label") or parsed_row.get("meal"),
        "name": parsed_row.get("name"),
        "calories": None,
        "protein": None,
        "carbs": None,
        "fat": None,
        "entry_id": parsed_row.get("entry_id"),
        "index": parsed_row.get("index"),
    }


def _take_named_match(
    parsed_rows: list[dict], used: list[bool], name: str | None
) -> dict | None:
    wanted = _entry_name_key(name)
    for position, candidate in enumerate(parsed_rows):
        if used[position]:
            continue
        if _entry_name_key(candidate.get("name")) == wanted:
            used[position] = True
            return candidate
    return None


def merge_entry_identities(cached: list[dict], parsed: list[dict]) -> list[dict]:
    """Attach diary-page entry ids onto cached rows.

    When a meal has the same number of rows in both lists, zip by position.
    The library's anchor text and this page's text can disagree, and both
    walks follow that meal's rows in order. A diary row the cache missed is
    kept so a duplicate name stays visible.
    """
    parsed_by_meal: dict[str | None, list[dict]] = {}
    for entry in parsed:
        parsed_by_meal.setdefault(entry.get("meal"), []).append(entry)

    cached_by_meal: dict[str | None, list[dict]] = {}
    meal_order: list[str | None] = []
    for row in cached:
        meal = _normalize_meal(row.get("meal"))
        if meal not in cached_by_meal:
            meal_order.append(meal)
        cached_by_meal.setdefault(meal, []).append(row)
    for meal in parsed_by_meal:
        if meal not in cached_by_meal:
            meal_order.append(meal)

    merged: list[dict] = []
    for meal in meal_order:
        cached_rows = cached_by_meal.get(meal, [])
        parsed_rows = parsed_by_meal.get(meal, [])
        if cached_rows and len(cached_rows) == len(parsed_rows):
            merged.extend(
                _with_identity(row, parsed_row)
                for row, parsed_row in zip(cached_rows, parsed_rows, strict=True)
            )
            continue
        used = [False] * len(parsed_rows)
        for row in cached_rows:
            merged.append(
                _with_identity(
                    row, _take_named_match(parsed_rows, used, row.get("name"))
                )
            )
        for parsed_row, taken in zip(parsed_rows, used, strict=True):
            if not taken:
                merged.append(_identity_only(parsed_row))
    return merged


def _meal_pool(entries: list[dict], meal: str | None) -> list[dict]:
    target = _normalize_meal(meal)
    return entries if target is None else [e for e in entries if e["meal"] == target]


def find_entries(
    entries: list[dict], query: str, meal: str | None = None
) -> list[dict]:
    needle = query.lower()
    return [e for e in _meal_pool(entries, meal) if needle in e["name"].lower()]


def remove_entry(client, entry_id: str, token: str) -> None:
    resp = client.session.post(
        parse.urljoin(client.BASE_URL_SECURE, f"food/remove/{entry_id}"),
        data={"_method": "delete", "authenticity_token": token},
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.myfitnesspal.com",
            "Referer": food_diary_url(client),
        },
    )
    if resp.status_code not in (200, 204):
        raise RuntimeError(
            f"MyFitnessPal /food/remove returned HTTP {resp.status_code}"
        )


class NoMatchingEntry(DiaryLookupError):
    pass


class AmbiguousEntry(DiaryLookupError):
    def __init__(
        self,
        query: str,
        meal: str | None,
        day: date,
        candidates: list[dict],
        advice: str = "Use a more specific query to pick one.",
    ):
        self.candidates = candidates
        where = f" in {meal}" if meal else ""
        options = "; ".join(_format_entry(candidate) for candidate in candidates)
        super().__init__(
            f"'{query}'{where} on {day.isoformat()} matches multiple diary entries: "
            f"{options}. {advice}"
        )


def resolve_entry(entries: list[dict], query: str, meal: str | None, day: date) -> dict:
    candidates = find_entries(entries, query, meal)
    exact = [c for c in candidates if c["name"].lower() == query.lower()]
    if len(exact) == 1:
        return exact[0]
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        raise AmbiguousEntry(query, meal, day, candidates)
    raise no_matching_entry(entries, query, meal, day)


def no_matching_entry(
    entries: list[dict], query: str, meal: str | None, day: date
) -> NoMatchingEntry:
    where = f" in {meal}" if meal else ""
    pool = _meal_pool(entries, meal)
    logged = (
        "; ".join(_format_entry(entry) for entry in pool)
        if pool
        else "(nothing logged)"
    )
    return NoMatchingEntry(
        f"no diary entry matching '{query}'{where} on {day.isoformat()}. "
        f"Entries actually logged{where}: {logged}"
    )


_DELETE_ONE_ADVICE = (
    "Nothing was deleted. Narrow the query to one entry, or pass entry_id "
    "to remove one row, or pass meal and index (the 1-based position of that "
    "row within the meal)."
)


def _clean_entry_id(entry_id: str | int | None) -> str | None:
    if entry_id is None or isinstance(entry_id, bool):
        return None
    text = str(entry_id).strip()
    return text or None


def _clean_index(index: int | None) -> int | None:
    if index is None:
        return None
    if isinstance(index, bool) or not isinstance(index, int) or index < 1:
        raise ValueError(
            "index must be an integer >= 1 (the first entry in the meal is 1)"
        )
    return index


def _query_matches(entry: dict, query: str | None) -> bool:
    if not query:
        return True
    return query.lower() in entry["name"].lower()


def _select_food_entry(
    entries: list[dict],
    day: date,
    query: str | None,
    meal: str | None,
    entry_id: str | None,
    index: int | None,
) -> dict:
    if entry_id is not None:
        found = [entry for entry in entries if str(entry.get("entry_id")) == entry_id]
        if not found:
            logged = (
                "; ".join(_format_entry(entry) for entry in entries)
                if entries
                else "(nothing logged)"
            )
            raise NoMatchingEntry(
                f"no diary entry with entry_id={entry_id} on {day.isoformat()}. "
                f"Entries actually logged: {logged}"
            )
        if len(found) > 1:
            raise AmbiguousEntry(entry_id, meal, day, found, advice=_DELETE_ONE_ADVICE)
        entry = found[0]
        if meal is not None and entry["meal"] != meal:
            raise DiaryLookupError(
                f"entry_id={entry_id} is in {entry['meal']!r}, not {meal!r}. "
                "Nothing was deleted."
            )
        if index is not None and entry.get("index") != index:
            raise DiaryLookupError(
                f"entry_id={entry_id} is index {entry.get('index')} in "
                f"{entry['meal']!r}, not index {index}. Nothing was deleted."
            )
        if not _query_matches(entry, query):
            raise DiaryLookupError(
                f"{_format_entry(entry)} on {day.isoformat()} does not match "
                f"{query!r}. Nothing was deleted."
            )
        return entry

    if index is not None:
        if meal is None:
            raise ValueError(
                "index is a position within one meal; pass meal as well "
                "(breakfast, lunch, dinner, snacks, or a meal name on the diary)"
            )
        pool = _meal_pool(entries, meal)
        found = [entry for entry in pool if entry.get("index") == index]
        if not found:
            logged = (
                "; ".join(_format_entry(entry) for entry in pool)
                if pool
                else "(nothing logged)"
            )
            raise NoMatchingEntry(
                f"no diary entry at index {index} in {meal} on {day.isoformat()}. "
                f"Entries in that meal: {logged}"
            )
        if len(found) > 1:
            raise AmbiguousEntry(
                query or str(index), meal, day, found, advice=_DELETE_ONE_ADVICE
            )
        entry = found[0]
        if query and not _query_matches(entry, query):
            logged = "; ".join(_format_entry(item) for item in pool)
            raise DiaryLookupError(
                f"index {index} in {meal} on {day.isoformat()} is "
                f"{_format_entry(entry)}, which does not match {query!r}. "
                f"Nothing was deleted. Entries in that meal: {logged}"
            )
        return entry

    try:
        return resolve_entry(entries, query or "", meal, day)
    except AmbiguousEntry as ambiguous:
        raise AmbiguousEntry(
            query or "",
            meal,
            day,
            ambiguous.candidates,
            advice=_DELETE_ONE_ADVICE,
        ) from None


def delete_food(
    client,
    day: date,
    query: str | None = None,
    meal: str | None = None,
    page: tuple | None = None,
    *,
    entry_id: str | int | None = None,
    index: int | None = None,
) -> dict:
    entry_id = _clean_entry_id(entry_id)
    index = _clean_index(index)
    if query is not None and not str(query).strip():
        query = None
    if query is None and entry_id is None and index is None:
        raise ValueError("pass query, entry_id, or index")
    doc, token = page or diary_page(client, day)
    label = _normalize_meal(resolve_meal(doc, meal)[1]) if meal else None
    entry = _select_food_entry(diary_entries(doc), day, query, label, entry_id, index)
    remove_entry(client, entry["entry_id"], token)
    return {
        "removed": entry["name"],
        "meal": entry["meal"],
        "entry_id": entry["entry_id"],
        "index": entry.get("index"),
    }


def set_weight(client, day: date, value: float) -> dict:
    """Value is in the account's display unit (kg or lbs) — the v2 API stores
    and echoes whatever unit the account is configured with."""
    resp = client.session.post(
        parse.urljoin(client.BASE_API_URL, "v2/measurements"),
        json={"items": [{"type": "Weight", "value": value, "date": day.isoformat()}]},
        headers=api_headers(
            client, {"Accept": "application/json", "Content-Type": "application/json"}
        ),
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"MyFitnessPal /v2/measurements returned HTTP {resp.status_code}"
        )
    item = resp.json()["items"][0]
    return {"day": item["date"], "weight": item["value"], "unit": item.get("unit")}


# MFP stores 1 cup as 240 mL and 1 fl oz as 29.5735 mL (verified 2026-09-28 via
# GET /food/water, matching MFP.Tools.UnitConverter.Water in the web app).
ML_PER_WATER_UNIT = {"ml": 1.0, "l": 1000.0, "cup": 240.0, "fl_oz": 29.5735}

WATER_UNIT_ALIASES = {
    "ml": "ml",
    "mls": "ml",
    "milliliter": "ml",
    "milliliters": "ml",
    "millilitre": "ml",
    "millilitres": "ml",
    "l": "l",
    "liter": "l",
    "liters": "l",
    "litre": "l",
    "litres": "l",
    "cup": "cup",
    "cups": "cup",
    "floz": "fl_oz",
    "oz": "fl_oz",
    "ounce": "fl_oz",
    "ounces": "fl_oz",
    "fluidounce": "fl_oz",
    "fluidounces": "fl_oz",
}


def normalize_water_unit(unit: str) -> str:
    compact = "".join(character for character in unit.lower() if character.isalnum())
    canonical = WATER_UNIT_ALIASES.get(compact)
    if canonical is None:
        accepted = ", ".join(ML_PER_WATER_UNIT)
        raise ValueError(f"unknown water unit {unit!r}; use one of: {accepted}")
    return canonical


def get_water_ml(client, day: date) -> float:
    resp = client.session.get(
        parse.urljoin(client.BASE_URL_SECURE, "food/water")
        + f"?date={day.isoformat()}",
        headers=api_headers(client, {"Accept": "application/json"}),
    )
    resp.raise_for_status()
    return float(resp.json()["item"]["milliliters"])


def log_water(
    client, day: date, amount: float, unit: str = "cup", replace: bool = False
) -> dict:
    """Adds `amount` to the day's water total, or with replace=True sets the
    total to `amount`. /food/water always takes the full daily total."""
    canonical_unit = normalize_water_unit(unit)
    if replace and amount < 0:
        raise ValueError("amount can't be negative")
    if not replace and amount <= 0:
        raise ValueError(
            "amount must be greater than zero; use replace=True to lower the total"
        )
    amount_ml = float(amount) * ML_PER_WATER_UNIT[canonical_unit]
    previous_ml = get_water_ml(client, day)
    water_ml = amount_ml if replace else previous_ml + amount_ml
    _, csrf = diary_page(client, day)
    resp = client.session.post(
        parse.urljoin(client.BASE_URL_SECURE, "food/water"),
        data={"milliliters": water_ml, "date": day.isoformat()},
        headers=api_headers(
            client,
            {
                "X-CSRF-Token": csrf,
                "Origin": "https://www.myfitnesspal.com",
                "Referer": food_diary_url(client),
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        ),
    )
    if resp.status_code not in (200, 201, 204):
        raise RuntimeError(f"MyFitnessPal /food/water returned HTTP {resp.status_code}")
    return {
        "day": day.isoformat(),
        "previous_ml": previous_ml,
        "water_ml": water_ml,
        "amount": amount,
        "unit": canonical_unit,
        "replaced": replace,
    }


def get_note(client, day: date) -> str | None:
    """Reads the day's free-text diary note (the 'Notes' box at the bottom of
    the food diary). MFP stores the body double-HTML-encoded; returns the
    decoded text, or None when the day has no note."""
    url = (
        parse.urljoin(client.BASE_URL_SECURE, "food/note") + f"?date={day.isoformat()}"
    )
    resp = client.session.get(
        url, headers=api_headers(client, {"Accept": "application/json"})
    )
    resp.raise_for_status()
    item = (resp.json() or {}).get("item") or {}
    body = item.get("body")
    if not body:
        return None
    return unescape(unescape(body)) or None


def set_note(client, day: date, body: str) -> dict:
    """Writes the day's diary note, replacing whatever was there. Mirrors the
    web app's Save Note request: a form POST with the page csrf token."""
    _, csrf = diary_page(client, day)
    resp = client.session.post(
        parse.urljoin(client.BASE_URL_SECURE, "food/note"),
        data={"body": body, "date": day.isoformat()},
        headers=api_headers(
            client,
            {
                "Accept": "*/*",
                "X-CSRF-Token": csrf,
                "Origin": "https://www.myfitnesspal.com",
                "Referer": food_diary_url(client),
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        ),
    )
    if resp.status_code not in (200, 201, 204):
        raise RuntimeError(f"MyFitnessPal /food/note returned HTTP {resp.status_code}")
    return {"day": day.isoformat(), "note": body}


def push_note(client, day: date, text: str, append: bool = False) -> dict:
    """Writes `text` as the day's diary note. With append=True, keeps the
    existing note and adds `text` on a new line."""
    if append:
        existing = get_note(client, day)
        if existing:
            text = f"{existing}\n{text}"
    return set_note(client, day, text)


def get_exercise(client, day: date) -> dict:
    sections = {}
    for section in client._get_exercises(day):
        sections[section.name.lower()] = section.get_as_list()
    return {"day": day.isoformat(), "exercise": sections}


def exercise_page(client, day: date):
    url = parse.urljoin(
        client.BASE_URL_SECURE,
        f"exercise/diary/{client.effective_username}?date={day.isoformat()}",
    )
    resp = client.session.get(url, headers=api_headers(client))
    resp.raise_for_status()
    doc = lh.fromstring(resp.text)
    tokens = doc.xpath("//meta[@name='csrf-token']/@content")
    if not tokens:
        raise RuntimeError("couldn't read the MyFitnessPal csrf token")
    return doc, tokens[0]


def _field_key(heading: str) -> str:
    words = "".join(c if c.isalnum() else " " for c in heading.lower()).split()
    return "_".join(words)


def _cell_number(cell) -> float | None:
    try:
        return float(cell.text_content().strip().replace(",", ""))
    except ValueError:
        return None


def _exercise_name(cell) -> str:
    for anchor in cell.xpath(".//a"):
        name = anchor.text_content().strip()
        if name:
            return name
    return cell.text_content().strip()


def exercise_entries(doc) -> list[dict]:
    """Deletable rows from every exercise section table. Each entry carries
    the section ("cardiovascular", "strength training") plus that section's
    own columns, e.g. minutes/calories_burned or sets/reps_set/weight_set."""
    entries = []
    for table in doc.xpath("//table[contains(@class, 'table0')]"):
        headings = table.xpath("./thead/tr[1]/td")
        if not headings:
            continue
        section = headings[0].text_content().strip().lower()
        field_keys = [_field_key(h.text_content()) for h in headings[1:]]
        for tr in table.xpath("./tbody/tr[not(@class)]"):
            delete_links = tr.xpath("./td[contains(@class, 'delete')]//a/@href")
            if not delete_links:
                continue
            cells = tr.xpath("./td")
            entry = {
                "entry_id": delete_links[0].split("?")[0].rstrip("/").split("/")[-1],
                "section": section,
                "name": _exercise_name(cells[0]),
            }
            for key, cell in zip(field_keys, cells[1:], strict=False):
                if key:
                    entry[key] = _cell_number(cell)
            entries.append(entry)
    return entries


def remove_exercise_entry(client, entry_id: str, token: str) -> None:
    resp = client.session.post(
        parse.urljoin(client.BASE_URL_SECURE, f"exercise/remove/{entry_id}"),
        data={"_method": "delete", "authenticity_token": token},
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.myfitnesspal.com",
            "Referer": parse.urljoin(
                client.BASE_URL_SECURE, f"exercise/diary/{client.effective_username}"
            ),
        },
    )
    if resp.status_code not in (200, 204):
        raise RuntimeError(
            f"MyFitnessPal /exercise/remove returned HTTP {resp.status_code}"
        )


def delete_exercise(client, day: date, query: str, all_matches: bool = False) -> dict:
    """Default: exactly one entry, resolved like delete_food. all_matches:
    every entry whose name contains `query` (e.g. Garmin's duplicate rows)."""
    doc, token = exercise_page(client, day)
    entries = exercise_entries(doc)
    if all_matches:
        targets = find_entries(entries, query)
        if not targets:
            raise no_matching_entry(entries, query, None, day)
    else:
        try:
            targets = [resolve_entry(entries, query, None, day)]
        except AmbiguousEntry as ambiguous:
            raise AmbiguousEntry(
                query,
                None,
                day,
                ambiguous.candidates,
                advice=(
                    "Use a more specific query to pick one, or pass "
                    "all_matches=True to remove every match (e.g. duplicate "
                    "rows from a sync)."
                ),
            ) from None
    for entry in targets:
        remove_exercise_entry(client, entry["entry_id"], token)
    return {"day": day.isoformat(), "removed": targets, "count": len(targets)}
