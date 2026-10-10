"""The user's own MyFitnessPal foods, saved meals, recipes, and recents.

The logged-in web app reads My Foods from GET /api/services/users/foods/mine
and saved meals from GET /api/services/users/meals/mine. Those BFF calls send
the NextAuth CSRF from GET /api/auth/csrf as X-CSRF-Token and do not send the
API bearer token. Recipes are still the legacy HTML pages /recipe_parser and
/recipe/view/{id}. Recent and frequent foods are the add-to-diary tabs
POST /food/load_recent and POST /food/load_most_used, which use the Rails
csrf from GET /food/add_to_diary.

/food/add rejects v2 food ids. A personal food that has a version is logged
with POST /api/services/diary. A recipe that only has a legacy food_id and
weight_id is one /food/add line, and quantity is the number of servings.
A saved meal is not copy_meal (that copies a diary section): each component
is logged, component quantity times the requested servings. Components that
arrive without ids are resolved by exact My Foods name first. If any
component cannot be resolved, nothing is written.
"""

from urllib import parse

from lxml import html as lh

from . import diary
from .food_ranking import normalize_query

PERSONAL_SOURCES = ("my_food", "my_meal", "my_recipe", "recent", "frequent")
LOGGABLE_SOURCES = frozenset(PERSONAL_SOURCES)
_MAX_PAGES = 5
# The add-to-diary recent/frequent tabs are per meal slot. The four default
# slots match breakfast, lunch, dinner, and snacks.
_RECENT_MEAL_SLOTS = ("0", "1", "2", "3")


class PersonalLookupError(diary.DiaryLookupError):
    """A personal food, recipe, or saved meal could not be resolved.

    DiaryLookupError so a message that mentions the login session does not
    trigger an auth refresh.
    """


def _number(value):
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _json_id(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = str(value).strip()
    return text or None


def _as_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "foods", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _unwrap(raw):
    if not isinstance(raw, dict):
        return raw
    item = raw.get("item")
    if not isinstance(item, dict):
        return raw
    if any(key in raw for key in ("description", "id", "food", "meal_id", "name")):
        return raw
    return item


def _macros(raw):
    if not isinstance(raw, dict):
        raw = {}
    contents = raw.get("nutritional_contents")
    source = contents if isinstance(contents, dict) else raw
    calories = _number(source.get("energy"))
    if calories is None:
        calories = _number(source.get("calories"))
    if calories is None and source is not raw:
        calories = _number(raw.get("calories"))
        if calories is None:
            calories = _number(raw.get("energy"))
    protein = _number(source.get("protein"))
    if protein is None:
        protein = _number(raw.get("protein"))
    carbs = _number(source.get("carbohydrates"))
    if carbs is None:
        carbs = _number(source.get("carbs"))
    if carbs is None:
        carbs = _number(raw.get("carbohydrates"))
    if carbs is None:
        carbs = _number(raw.get("carbs"))
    fat = _number(source.get("fat"))
    if fat is None:
        fat = _number(raw.get("fat"))
    return {
        "calories": calories,
        "protein": protein,
        "carbs": carbs,
        "fat": fat,
    }


def _servings(food):
    servings = []
    for size in food.get("serving_sizes") or []:
        if not isinstance(size, dict):
            continue
        multiplier = _number(size.get("nutrition_multiplier"))
        servings.append(
            {
                "id": _json_id(size.get("id")),
                "value": size.get("value"),
                "unit": size.get("unit"),
                "nutrition_multiplier": multiplier,
                "label": diary.serving_label(size),
            }
        )
    return servings


def _blank_to_none(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _food_record(raw, source):
    raw = _unwrap(raw)
    if not isinstance(raw, dict):
        return None
    weight = raw.get("weight") if isinstance(raw.get("weight"), dict) else None
    food = raw.get("food") if isinstance(raw.get("food"), dict) else raw
    name = _blank_to_none(food.get("description") or food.get("name"))
    if name is None:
        return None
    macros = _macros(food)
    if food is not raw:
        outer = _macros(raw)
        for key, value in outer.items():
            if macros[key] is None:
                macros[key] = value
    servings = _servings(food)
    serving = servings[0]["label"] if servings else None
    weight_id = None
    if weight is not None and weight.get("id") is not None:
        weight_id = _json_id(weight.get("id"))
        label = diary.serving_label(weight)
        if not servings:
            servings = [
                {
                    "id": weight_id,
                    "value": weight.get("value"),
                    "unit": weight.get("unit"),
                    "nutrition_multiplier": _number(weight.get("nutrition_multiplier")),
                    "label": label,
                }
            ]
        serving = label or serving
    elif servings and servings[0]["id"] is not None:
        weight_id = servings[0]["id"]
    verified = food.get("verified")
    if not isinstance(verified, bool):
        verified = None
    return {
        "source": source,
        "name": name,
        "brand": _blank_to_none(food.get("brand_name")),
        "calories": macros["calories"],
        "protein": macros["protein"],
        "carbs": macros["carbs"],
        "fat": macros["fat"],
        "serving": serving,
        "servings": servings,
        "verified": verified,
        "food_id": _json_id(food.get("id")),
        "weight_id": weight_id,
        "version": _json_id(food.get("version")),
        "meal_id": None,
        "recipe_id": None,
        "quantity": _number(raw.get("quantity")) if food is not raw else None,
    }


def _name_hit(name, query):
    """Exact name, or a close name when the query is at least 3 characters."""
    wanted = normalize_query(query)
    if not wanted:
        return False
    hay = normalize_query(name or "")
    if hay == wanted:
        return True
    if len(wanted) < 3:
        return False
    return wanted in hay or hay in wanted


def _list_hit(name, search):
    if not search:
        return True
    wanted = normalize_query(search)
    hay = normalize_query(name or "")
    return wanted in hay or hay in wanted


def _bff_csrf(client) -> str:
    cached = getattr(client, "_bff_csrf", None)
    if cached:
        return cached
    url = parse.urljoin(client.BASE_URL_SECURE, "api/auth/csrf")
    resp = client.session.get(url, headers={"Accept": "application/json"})
    resp.raise_for_status()
    token = (resp.json() or {}).get("csrfToken")
    if not token:
        raise RuntimeError("couldn't read the MyFitnessPal csrf token")
    client._bff_csrf = token
    return token


def _rails_csrf(client) -> str:
    cached = getattr(client, "_rails_csrf", None)
    if cached:
        return cached
    url = parse.urljoin(client.BASE_URL_SECURE, "food/add_to_diary")
    resp = client.session.get(url, headers=diary.api_headers(client))
    resp.raise_for_status()
    doc = lh.fromstring(resp.text)
    tokens = doc.xpath("//meta[@name='csrf-token']/@content")
    if not tokens:
        raise RuntimeError("couldn't read the MyFitnessPal csrf token")
    client._rails_csrf = tokens[0]
    return tokens[0]


def _bff_headers(client, extra=None):
    headers = {
        "Accept": "application/json",
        "X-CSRF-Token": _bff_csrf(client),
        "X-Requested-With": "XMLHttpRequest",
    }
    if extra:
        headers.update(extra)
    return headers


def _bff_get(client, path, params):
    query = parse.urlencode(params)
    url = parse.urljoin(client.BASE_URL_SECURE, path)
    if query:
        url = f"{url}?{query}"
    resp = client.session.get(url, headers=_bff_headers(client))
    resp.raise_for_status()
    return resp.json()


def _cap(records, limit):
    if limit is None:
        return records
    return records[:limit]


def list_my_foods(client, search="", limit=50):
    payload = _bff_get(client, "api/services/users/foods/mine", {"search": search})
    records = []
    for raw in _as_list(payload):
        record = _food_record(raw, "my_food")
        if record is None or not _list_hit(record["name"], search):
            continue
        records.append(record)
    return _cap(records, limit)


def _meal_component(raw):
    if not isinstance(raw, dict):
        return None
    quantity = _number(raw.get("quantity"))
    inner = raw["food"] if isinstance(raw.get("food"), dict) else raw
    record = _food_record(inner, "my_food")
    if record is None:
        return None
    record["quantity"] = 1.0 if quantity is None else quantity
    return record


def _sum_macro(foods, key):
    total = 0.0
    seen = False
    for food in foods:
        value = food.get(key)
        if value is None:
            continue
        seen = True
        total += value * (food.get("quantity") or 1.0)
    return total if seen else None


def list_my_meals(client, search="", limit=50):
    payload = _bff_get(
        client, "api/services/users/meals/mine", {"limit": 100, "search": search}
    )
    meals = []
    for raw in _as_list(payload):
        meal = _unwrap(raw)
        if not isinstance(meal, dict):
            continue
        name = _blank_to_none(meal.get("description") or meal.get("name"))
        if name is None or not _list_hit(name, search):
            continue
        foods = []
        for component in meal.get("foods") or []:
            parsed = _meal_component(component)
            if parsed is not None:
                foods.append(parsed)
        meal_id = _json_id(meal.get("meal_id") or meal.get("id"))
        meals.append(
            {
                "source": "my_meal",
                "name": name,
                "brand": None,
                "calories": _sum_macro(foods, "calories"),
                "protein": _sum_macro(foods, "protein"),
                "carbs": _sum_macro(foods, "carbs"),
                "fat": _sum_macro(foods, "fat"),
                "serving": "1 meal",
                "servings": [
                    {
                        "id": None,
                        "value": 1,
                        "unit": "meal",
                        "nutrition_multiplier": 1.0,
                        "label": "1 meal",
                    }
                ],
                "verified": None,
                "food_id": None,
                "weight_id": None,
                "version": None,
                "meal_id": meal_id,
                "recipe_id": None,
                "quantity": 1.0,
                "foods": foods,
            }
        )
    return _cap(meals, limit)


def _xpath_text(doc, xpath):
    nodes = doc.xpath(xpath)
    if not nodes:
        return None
    node = nodes[0]
    text = node if isinstance(node, str) else node.text_content()
    text = " ".join(str(text).split())
    return text or None


def _first_number(text):
    if not text:
        return None
    token = []
    for character in text:
        if character.isdigit() or character == "." or (character == "-" and not token):
            token.append(character)
        elif token:
            break
    return _number("".join(token)) if token else None


def _recipe_calories(item):
    info = item.xpath(".//p[@class='search-nutritional-info']")
    if not info or not info[0].text:
        return None, None
    parts = [part.strip() for part in info[0].text.split(",")]
    serving = parts[-2] if len(parts) >= 2 and parts[-2] else None
    calories = None
    if parts:
        calories = _first_number(parts[-1].replace("calories", ""))
    return serving, calories


def _parse_recipe_list(document):
    recipes = []
    for item in document.xpath("//*[@id='main']/ul[1]/li"):
        links = item.xpath("./div[2]/h2/span[1]/a")
        if not links:
            links = item.xpath(".//a[contains(@href, '/recipe/view/')]")
        if not links:
            continue
        link = links[0]
        href = link.get("href") or ""
        marker = "/recipe/view/"
        if marker not in href:
            continue
        recipe_id = (
            href.split(marker, 1)[1].split("?", 1)[0].split("#", 1)[0].strip("/")
        )
        if not recipe_id:
            continue
        name = _blank_to_none(link.get("title")) or _blank_to_none(link.text_content())
        if name is None:
            continue
        weight_raw = link.get("data-weight-ids") or item.get("data-weight-ids") or ""
        weight_ids = [part for part in weight_raw.split(",") if part]
        serving, calories = _recipe_calories(item)
        recipes.append(
            {
                "source": "my_recipe",
                "name": name,
                "brand": None,
                "calories": calories,
                "protein": _number(
                    item.get("data-protein") or link.get("data-protein")
                ),
                "carbs": _number(item.get("data-carbs") or link.get("data-carbs")),
                "fat": _number(item.get("data-fat") or link.get("data-fat")),
                "serving": serving,
                "servings": [],
                "verified": None,
                "food_id": _json_id(
                    link.get("data-original-id") or item.get("data-original-id")
                ),
                "weight_id": _json_id(weight_ids[0]) if weight_ids else None,
                "version": None,
                "meal_id": None,
                "recipe_id": recipe_id,
                "quantity": None,
                "recipe_servings": _number(item.get("data-servings")),
            }
        )
    return recipes


def _recipe_has_next(document, page):
    links = document.xpath('//*[@id="main"]/ul[2]/a')
    if not links:
        return False
    if page == 1:
        return True
    return len(links) > 1


def list_my_recipes(client, search="", limit=50):
    collected = []
    seen = set()
    for page in range(1, _MAX_PAGES + 1):
        url = parse.urljoin(
            client.BASE_URL_SECURE, f"recipe_parser?page={page}&sort_order=recent"
        )
        resp = client.session.get(url, headers=diary.api_headers(client))
        resp.raise_for_status()
        document = lh.fromstring(resp.text)
        batch = _parse_recipe_list(document)
        fresh = 0
        for recipe in batch:
            if recipe["recipe_id"] in seen:
                continue
            seen.add(recipe["recipe_id"])
            fresh += 1
            if not _list_hit(recipe["name"], search):
                continue
            collected.append(recipe)
            if limit is not None and len(collected) >= limit:
                return collected
        if not batch or fresh == 0 or not _recipe_has_next(document, page):
            break
    return collected


def parse_recipe_view(html):
    document = lh.fromstring(html)
    food_ids = document.xpath("//input[@name='food_entry[food_id]']/@value")
    weight_ids = document.xpath("//input[@name='food_entry[weight_id]']/@value")
    return {
        "name": _xpath_text(document, '//*[@id="main"]/div[3]/div[2]/h1'),
        "recipe_servings": _first_number(
            _xpath_text(document, '//*[@id="recipe_servings"]')
        ),
        "calories": _first_number(
            _xpath_text(document, '//*[@id="main"]/div[3]/div[2]/div[2]/div')
        ),
        "protein": _first_number(
            _xpath_text(document, '//*[@id="protein"]/td[1]/span[2]')
        ),
        "carbs": _first_number(_xpath_text(document, '//*[@id="carbs"]/td[1]/span[2]')),
        "fat": _first_number(
            _xpath_text(document, '//*[@id="total_fat"]/td[1]/span[2]')
        ),
        "food_id": _json_id(food_ids[0]) if food_ids else None,
        "weight_id": _json_id(weight_ids[0]) if weight_ids else None,
    }


def _recipe_view(client, recipe_id):
    url = parse.urljoin(
        client.BASE_URL_SECURE, f"recipe/view/{parse.quote(str(recipe_id))}"
    )
    resp = client.session.get(url, headers=diary.api_headers(client))
    resp.raise_for_status()
    return parse_recipe_view(resp.text)


def _load_tab(client, endpoint, source, limit):
    token = _rails_csrf(client)
    collected = []
    seen = set()
    url = parse.urljoin(client.BASE_URL_SECURE, endpoint)
    headers = diary.api_headers(
        client,
        {
            "Accept": "application/json",
            "X-CSRF-Token": token,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
    )
    for meal in _RECENT_MEAL_SLOTS:
        base_index = 0
        for page in range(1, _MAX_PAGES + 1):
            resp = client.session.post(
                url,
                data={"meal": meal, "base_index": str(base_index), "page": str(page)},
                headers=headers,
            )
            resp.raise_for_status()
            items = _as_list(resp.json())
            if not items:
                break
            stop_page = False
            for raw in items:
                record = _food_record(raw, source)
                if record is None or record.get("food_id") is None:
                    continue
                key = str(record["food_id"])
                if key in seen:
                    stop_page = True
                    break
                seen.add(key)
                collected.append(record)
                if limit is not None and len(collected) >= limit:
                    return collected
            base_index += len(items)
            if stop_page:
                break
    return collected


def list_recent_foods(client, limit=50):
    return _load_tab(client, "food/load_recent", "recent", limit)


def list_frequent_foods(client, limit=50):
    return _load_tab(client, "food/load_most_used", "frequent", limit)


def _public_result(result):
    return {
        "source": "public",
        "name": result["name"],
        "brand": result.get("brand"),
        "calories": result.get("calories"),
        "protein": result.get("protein"),
        "carbs": result.get("carbs"),
        "fat": result.get("fat"),
        "serving": result.get("serving"),
        "servings": [],
        "verified": result.get("verified"),
        "food_id": result.get("food_id"),
        "weight_id": result.get("weight_id"),
        "version": None,
        "meal_id": None,
        "recipe_id": None,
        "quantity": None,
    }


def matching_items(client, query):
    """Personal foods, meals, recipes, recents, and frequents that fit `query`.

    Recents and frequents that repeat a food id already returned by an earlier
    source are dropped. The public database is not queried.
    """
    groups = (
        list_my_foods(client, limit=None),
        list_my_meals(client, limit=None),
        list_my_recipes(client, limit=None),
        list_recent_foods(client, limit=None),
        list_frequent_foods(client, limit=None),
    )
    ordered = []
    seen = set()
    for items in groups:
        for item in items:
            if not _name_hit(item["name"], query):
                continue
            food_id = item.get("food_id")
            key = None if food_id is None else str(food_id)
            if item["source"] in ("recent", "frequent") and key in seen:
                continue
            if key is not None and item["source"] in ("my_food", "recent", "frequent"):
                seen.add(key)
            ordered.append(item)
    return ordered


def choose(matches, query):
    """Pick one personal match, or report that the query is ambiguous.

    Returns (item, ambiguous). An exact name in the earliest source wins.
    Otherwise one close name in the earliest source wins. Two matches in that
    source are ambiguous and later sources are not consulted. No match returns
    (None, False) so the caller can try the public database.
    """
    by_source = {source: [] for source in PERSONAL_SOURCES}
    for item in matches:
        by_source.setdefault(item["source"], []).append(item)
    wanted = normalize_query(query)

    def pick(predicate):
        for source in PERSONAL_SOURCES:
            hits = [item for item in by_source.get(source, []) if predicate(item)]
            if len(hits) == 1:
                return hits[0], False
            if len(hits) > 1:
                return None, True
        return None, False

    chosen, ambiguous = pick(lambda item: normalize_query(item["name"]) == wanted)
    if chosen is not None or ambiguous:
        return chosen, ambiguous
    if len(wanted) < 3:
        return None, False
    return pick(lambda item: _name_hit(item["name"], query))


def find_food(client, query, limit=5, with_macros=True):
    personal = matching_items(client, query)
    seen = {
        str(item["food_id"]) for item in personal if item.get("food_id") is not None
    }
    public = []
    for result in diary.search_food(client, query, limit, with_macros):
        food_id = result.get("food_id")
        if food_id is not None and str(food_id) in seen:
            continue
        public.append(_public_result(result))
    return (personal + public)[:limit]


def _meal_page(client, day, meal, page):
    doc, token = page if page is not None else diary.diary_page(client, day)
    position, _label = diary.resolve_meal(doc, meal)
    return int(position), (doc, token)


def _serving_size(item):
    servings = item.get("servings") or []
    first = servings[0] if servings else {}
    value = first.get("value")
    multiplier = first.get("nutrition_multiplier")
    return {
        "nutrition_multiplier": 1 if multiplier is None else multiplier,
        "unit": first.get("unit") or "serving",
        "value": 1 if value is None else value,
    }


def _log_result(item, day, meal, quantity, source):
    return {
        "logged": item["name"],
        "food_id": item.get("food_id"),
        "weight_id": item.get("weight_id"),
        "version": item.get("version"),
        "serving": item.get("serving"),
        "quantity": quantity,
        "meal": meal,
        "date": day.isoformat(),
        "source": source,
    }


def log_diary_api(client, day, meal_position, item, quantity):
    if not item.get("food_id") or not item.get("version"):
        raise PersonalLookupError(
            f"{item.get('name') or 'food'!r} has no id and version to log"
        )
    resp = client.session.post(
        parse.urljoin(client.BASE_URL_SECURE, "api/services/diary"),
        json={
            "items": [
                {
                    "type": "food_entry",
                    "date": day.isoformat(),
                    "food": {"id": item["food_id"], "version": item["version"]},
                    "servings": quantity,
                    "meal_position": meal_position,
                    "serving_size": _serving_size(item),
                }
            ]
        },
        headers=_bff_headers(client, {"Content-Type": "application/json"}),
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"MyFitnessPal /api/services/diary returned HTTP {resp.status_code}"
        )


def log_library_food(client, item, day, meal, quantity, page=None):
    position, page = _meal_page(client, day, meal, page)
    if item.get("version"):
        log_diary_api(client, day, position, item, quantity)
    elif item.get("food_id") and item.get("weight_id"):
        diary.push_food(
            client, day, meal, item["food_id"], item["weight_id"], quantity, page
        )
    else:
        raise PersonalLookupError(
            f"{item.get('name') or 'food'!r} has no id to log in this session"
        )
    return _log_result(item, day, meal, quantity, item.get("source") or "my_food")


def _require(items, key, value, label):
    wanted = str(value)
    for item in items:
        if item.get(key) is not None and str(item[key]) == wanted:
            return item
    raise PersonalLookupError(f"no {label} with id {value!r} in this session")


def log_my_food(client, food_id, day, meal, quantity, page=None):
    item = _require(list_my_foods(client, limit=None), "food_id", food_id, "My Food")
    return log_library_food(client, item, day, meal, quantity, page)


def _with_recipe_ids(client, item):
    if item.get("version") or (item.get("food_id") and item.get("weight_id")):
        return item
    details = _recipe_view(client, item["recipe_id"])
    merged = dict(item)
    for key in ("food_id", "weight_id", "calories", "protein", "carbs", "fat"):
        if merged.get(key) is None and details.get(key) is not None:
            merged[key] = details[key]
    if not merged.get("name") and details.get("name"):
        merged["name"] = details["name"]
    return merged


def log_recipe(client, recipe_id, day, meal, quantity, page=None):
    item = _require(
        list_my_recipes(client, limit=None), "recipe_id", recipe_id, "My Recipe"
    )
    item = _with_recipe_ids(client, item)
    if not item.get("version") and not (item.get("food_id") and item.get("weight_id")):
        raise PersonalLookupError(f"recipe {recipe_id!r} has no food id to log")
    return log_library_food(client, item, day, meal, quantity, page)


def _can_log(item):
    if not item.get("food_id"):
        return False
    return bool(item.get("version") or item.get("weight_id"))


def _resolve_component(component, my_foods, meal_name):
    if _can_log(component):
        return component
    wanted = normalize_query(component["name"])
    exact = [food for food in my_foods if normalize_query(food["name"]) == wanted]
    if len(exact) != 1 or not _can_log(exact[0]):
        raise PersonalLookupError(
            f"saved meal {meal_name!r} includes {component['name']!r}, "
            "which is not one My Food"
        )
    resolved = dict(exact[0])
    resolved["quantity"] = component.get("quantity") or 1.0
    return resolved


def log_saved_meal(client, meal_id, day, meal, quantity, page=None):
    item = _require(list_my_meals(client, limit=None), "meal_id", meal_id, "saved meal")
    my_foods = None
    resolved = []
    for component in item["foods"]:
        if not _can_log(component) and my_foods is None:
            my_foods = list_my_foods(client, limit=None)
        resolved.append(_resolve_component(component, my_foods or [], item["name"]))
    if not resolved:
        raise PersonalLookupError(f"saved meal {item['name']!r} has no foods to log")
    position, page = _meal_page(client, day, meal, page)
    logged_items = []
    for component in resolved:
        component_quantity = (component.get("quantity") or 1.0) * quantity
        if component.get("version"):
            log_diary_api(client, day, position, component, component_quantity)
            logged_items.append(
                _log_result(component, day, meal, component_quantity, "my_food")
            )
        else:
            diary.push_food(
                client,
                day,
                meal,
                component["food_id"],
                component["weight_id"],
                component_quantity,
                page,
            )
            logged_items.append(
                _log_result(component, day, meal, component_quantity, "my_food")
            )
    return {
        "logged": item["name"],
        "source": "my_meal",
        "meal_id": item["meal_id"],
        "quantity": quantity,
        "meal": meal,
        "date": day.isoformat(),
        "items": logged_items,
    }


def log_match(client, item, day, meal, quantity, page=None):
    if item.get("source") == "my_meal":
        return log_saved_meal(client, item["meal_id"], day, meal, quantity, page)
    if item.get("source") == "my_recipe":
        return log_recipe(client, item["recipe_id"], day, meal, quantity, page)
    return log_library_food(client, item, day, meal, quantity, page)
