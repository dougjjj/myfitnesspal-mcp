import asyncio
import datetime
import json
from pathlib import Path

import pytest

from myfitnesspal_mcp import food_logging, mfp_client, personal, server
from myfitnesspal_mcp.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = datetime.date(2026, 7, 8)


def _load(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def library(client, make_response):
    client.session.route(
        "GET", "users/foods/mine", make_response(json_data=_load("my_foods.json"))
    )
    client.session.route(
        "GET", "users/meals/mine", make_response(json_data=_load("my_meals.json"))
    )
    client.session.route(
        "GET",
        "recipe_parser",
        make_response(text=(FIXTURES / "recipes.html").read_text()),
    )
    client.session.route(
        "GET",
        "recipe/view/",
        make_response(text=(FIXTURES / "recipe_view.html").read_text()),
    )
    client.session.route(
        "POST",
        "recipe/log_recipe",
        make_response(json_data={"status": "ok"}),
    )
    client.session.route(
        "POST", "food/load_recent", make_response(json_data=_load("recent_foods.json"))
    )
    client.session.route(
        "POST",
        "food/load_most_used",
        make_response(json_data=_load("frequent_foods.json")),
    )
    return client


def _diary_writes(client):
    writes = []
    for method, url, payload in client.session.calls:
        if method != "POST":
            continue
        path = url.split("?", 1)[0].rstrip("/")
        if path.endswith("/food/add") or path.endswith("/api/services/diary"):
            writes.append((method, url, payload))
    return writes


def _diary_posts(client):
    return [call for call in _diary_writes(client) if "api/services/diary" in call[1]]


def _food_add_posts(client):
    return [
        call
        for call in _diary_writes(client)
        if call[1].split("?", 1)[0].rstrip("/").endswith("/food/add")
    ]


def test_my_food_lists_name_macros_and_log_ids(library):
    foods = personal.list_my_foods(library)
    tea = next(food for food in foods if food["food_id"] == "tea-1")
    assert tea["name"] == "English Breakfast Tea with Milk"
    assert tea["source"] == "my_food"
    assert tea["brand"] is None
    assert tea["calories"] == 6
    assert tea["protein"] == 0.2
    assert tea["carbs"] == 1
    assert tea["fat"] == 0.1
    assert tea["serving"] == "1 serving"
    assert tea["version"] == "tea-v1"
    assert tea["weight_id"] == "w-tea"
    assert tea["servings"][0]["unit"] == "serving"


def test_recent_food_keeps_last_logged_quantity(library):
    recent = personal.list_recent_foods(library)
    tea = next(food for food in recent if food["food_id"] == "tea-1")
    assert tea["source"] == "recent"
    assert tea["quantity"] == 2
    assert tea["calories"] == 6


def test_recipe_list_includes_macros_and_log_ids(library):
    recipes = personal.list_my_recipes(library)
    pancakes = recipes[0]
    assert pancakes["name"] == "Homemade Pancakes"
    assert pancakes["recipe_id"] == "pancakes-1"
    assert pancakes["calories"] == 320
    assert pancakes["protein"] == 8
    assert pancakes["carbs"] == 40
    assert pancakes["fat"] == 12
    assert pancakes["serving"] == "1 serving"
    assert pancakes["recipe_servings"] == 8
    assert pancakes["food_id"] == "9001"
    assert pancakes["weight_id"] == "77"
    pages = [url for _, url, _ in library.session.calls if "recipe_parser" in url]
    assert len(pages) == 1


def test_recipe_view_reads_logger_nutrition_without_food_ids():
    parsed = personal.parse_recipe_view((FIXTURES / "recipe_view.html").read_text())
    assert parsed["name"] == "Homemade Pancakes"
    assert parsed["calories"] == 240
    assert parsed["recipe_servings"] == 4
    assert parsed["protein"] == 8
    assert parsed["carbs"] == 30
    assert parsed["fat"] == 6
    assert parsed["food_id"] is None
    assert parsed["weight_id"] is None


def test_find_ranks_personal_sources_before_public(library):
    tea, warnings = personal.find_food(library, "tea")
    assert warnings == []
    assert tea[0]["source"] == "my_food"
    assert tea[0]["food_id"] == "tea-1"
    assert [item["food_id"] for item in tea if item.get("food_id") == "tea-1"] == [
        "tea-1"
    ]

    pancakes, _warnings = personal.find_food(library, "pancake")
    assert [(item["source"], item["name"]) for item in pancakes[:3]] == [
        ("my_recipe", "Homemade Pancakes"),
        ("recent", "Pancake syrup"),
        ("frequent", "Pancake mix"),
    ]
    assert all(
        item["source"] != "public" or "pancake" not in item["name"].lower()
        for item in pancakes
    )

    banana, _warnings = personal.find_food(library, "banana")
    assert banana[0]["source"] == "public"
    assert banana[0]["name"] == "Banana"
    assert banana[0]["food_id"] == "111"


def test_log_my_food_uses_diary_api(library):
    result = personal.log_my_food(library, "tea-1", TODAY, "breakfast", 2)
    assert result["logged"] == "English Breakfast Tea with Milk"
    assert result["source"] == "my_food"
    assert result["quantity"] == 2
    posts = _diary_posts(library)
    assert len(posts) == 1
    _method, _url, kwargs = posts[0]
    item = kwargs["json"]["items"][0]
    assert item["type"] == "food_entry"
    assert item["food"] == {"id": "tea-1", "version": "tea-v1"}
    assert item["servings"] == 2
    assert item["meal_position"] == 0
    assert item["serving_size"]["unit"] == "serving"
    assert kwargs["headers"]["X-CSRF-Token"] == "csrf-test"
    assert "Authorization" not in kwargs["headers"]
    assert _food_add_posts(library) == []


def _form(data):
    return dict(data)


def test_log_recipe_posts_the_logger_payload(library):
    result = personal.log_recipe(library, "pancakes-1", TODAY, "breakfast", 2)
    assert result["source"] == "my_recipe"
    assert result["logged"] == "Homemade Pancakes"
    assert result["quantity"] == 2
    assert result["calories"] == 240
    assert result["recipe_servings"] == 4
    assert result["protein"] == 8
    posts = [
        call
        for call in library.session.calls
        if call[0] == "POST"
        and call[1].split("?", 1)[0].rstrip("/").endswith("/recipe/log_recipe")
    ]
    assert len(posts) == 1
    form = _form(posts[0][2]["data"])
    assert form["type"] == "log"
    assert form["servings"] == "2"
    assert form["meal"] == "0"
    assert form["date"] == "2026-07-08"
    assert form["recipe_servings"] == ""
    assert form["authenticity_token"] == "csrf-recipe"
    assert form["recipe[id]"] == "pancakes-1"
    assert form["recipe[name]"] == "Homemade Pancakes"
    assert form["recipe[servings]"] == "4"
    assert form["recipe[nutritional_contents_per_serving][energy][value]"] == "240"
    assert form["recipe[ingredients][0][food_id]"] == "flour-1"
    assert posts[0][2]["headers"]["X-CSRF-Token"] == "csrf-recipe"
    assert "Authorization" not in posts[0][2]["headers"]
    assert _food_add_posts(library) == []
    assert _diary_posts(library) == []


def test_recipe_without_list_ids_uses_the_logger(library, make_response):
    html = """
    <html><body><div id="main"><ul>
    <li><div></div><div><h2><span>
    <a href="/recipe/view/pancakes-1" title="Homemade Pancakes">Homemade Pancakes</a>
    </span></h2>
    <div class="calories"><span class="per-serving">240</span> calories</div>
    <label class="recipe-servings">4</label>
    </div></li>
    </ul></div></body></html>
    """
    library.session.route("GET", "recipe_parser", make_response(text=html))
    listed = personal.list_my_recipes(library)
    assert listed[0]["food_id"] is None
    assert listed[0]["calories"] == 240
    assert listed[0]["recipe_servings"] == 4
    result = personal.log_recipe(library, "pancakes-1", TODAY, "lunch", 1)
    assert result["recipe_id"] == "pancakes-1"
    assert result["calories"] == 240
    assert result["meal"] == "lunch"
    assert any("recipe/view/pancakes-1" in url for _, url, _ in library.session.calls)
    assert any(
        url.endswith("/recipe/log_recipe") for _, url, _ in library.session.calls
    )
    assert _food_add_posts(library) == []


def test_saved_meal_logs_every_component(library):
    result = personal.log_saved_meal(library, "meal-77", TODAY, "breakfast", 1)
    assert result["logged"] == "Weekend Breakfast"
    assert result["source"] == "my_meal"
    assert [item["food_id"] for item in result["items"]] == ["tea-1", "oats-1"]
    posts = _diary_posts(library)
    assert len(posts) == 2
    tea = posts[0][2]["json"]["items"][0]
    oats = posts[1][2]["json"]["items"][0]
    assert tea["food"] == {"id": "tea-1", "version": "tea-v1"}
    assert tea["servings"] == 2
    assert oats["food"] == {"id": "oats-1", "version": "oats-v1"}
    assert oats["servings"] == 1
    assert oats["serving_size"]["unit"] == "jar"
    assert _food_add_posts(library) == []


def test_saved_meal_quantity_scales_each_component(library):
    personal.log_saved_meal(library, "meal-77", TODAY, "breakfast", 2)
    posts = _diary_posts(library)
    assert posts[0][2]["json"]["items"][0]["servings"] == 4
    assert posts[1][2]["json"]["items"][0]["servings"] == 2


def test_unresolvable_meal_component_writes_nothing(library, make_response):
    library.session.route(
        "GET",
        "users/meals/mine",
        make_response(
            json_data=[
                {
                    "meal_id": "meal-bad",
                    "description": "Broken Plate",
                    "foods": [{"description": "Missing Food", "calories": 10}],
                }
            ]
        ),
    )
    with pytest.raises(personal.PersonalLookupError, match="Missing Food"):
        personal.log_saved_meal(library, "meal-bad", TODAY, "breakfast", 1)
    assert _diary_writes(library) == []


def test_unknown_saved_meal_is_not_an_auth_error(library):
    with pytest.raises(personal.PersonalLookupError, match="session") as caught:
        personal.log_saved_meal(library, "missing", TODAY, "breakfast", 1)
    assert mfp_client.is_auth_error(caught.value) is False
    assert _diary_writes(library) == []


def test_query_prefers_exact_personal_food_over_public(library, store):
    result = food_logging.log_by_query(
        library,
        store,
        "English Breakfast Tea with Milk",
        TODAY,
        "breakfast",
        2,
    )
    assert result["source"] == "my_food"
    assert result["food_id"] == "tea-1"
    assert result["quantity"] == 2
    assert not any("food/search" in url for _, url, _ in library.session.calls)


def test_query_close_match_logs_recipe(library, store):
    result = food_logging.log_by_query(
        library, store, "pancakes", TODAY, "breakfast", 2
    )
    assert result["source"] == "my_recipe"
    assert result["recipe_id"] == "pancakes-1"
    assert result["calories"] == 240
    logged = [
        call
        for call in library.session.calls
        if call[0] == "POST" and "recipe/log_recipe" in call[1]
    ]
    assert _form(logged[0][2]["data"])["servings"] == "2"
    assert not any("food/search" in url for _, url, _ in library.session.calls)


def test_query_exact_meal_logs_every_item(library, store):
    result = food_logging.log_by_query(
        library, store, "Weekend Breakfast", TODAY, "breakfast", 1
    )
    assert result["source"] == "my_meal"
    assert len(_diary_posts(library)) == 2
    assert not any("food/search" in url for _, url, _ in library.session.calls)


def test_ambiguous_personal_query_logs_nothing(library, store):
    result = food_logging.log_by_query(library, store, "tea", TODAY, "breakfast", 1)
    assert result["needs_choice"] is True
    assert result["logged"] is None
    names = {item["name"] for item in result["personal_matches"]}
    assert "English Breakfast Tea with Milk" in names
    assert "Green Tea" in names
    assert _diary_writes(library) == []


def test_pin_beats_a_personal_exact_match(library, store):
    store.set_pin("English Breakfast Tea with Milk", "555", "77", "Pinned tea", "1 cup")
    result = food_logging.log_by_query(
        library, store, "English Breakfast Tea with Milk", TODAY, "breakfast", 1
    )
    assert result["source"] == "pin"
    assert result["food_id"] == "555"
    assert not any("users/foods/mine" in url for _, url, _ in library.session.calls)
    assert _diary_posts(library) == []


def test_public_exact_still_logs_when_nothing_personal_matches(library, store):
    result = food_logging.log_by_query(library, store, "banana", TODAY, "breakfast", 1)
    assert result["source"] == "exact_match"
    assert result["food_id"] == "111"
    assert result["weight_id"] == "10"


def test_personal_reads_count_as_read_tools():
    for name in (
        "fitness_find_food",
        "fitness_list_my_foods",
        "fitness_list_my_meals",
        "fitness_list_my_recipes",
        "fitness_list_recent_foods",
        "fitness_list_frequent_foods",
    ):
        assert name in server.READ_TOOLS
        assert name not in server.WRITE_TOOLS
    assert "fitness_log_food" in server.FOOD_WRITE_TOOLS


@pytest.fixture
def connected(library, tmp_path, monkeypatch):
    monkeypatch.setenv("MFP_ALLOW_WRITES", "1")
    monkeypatch.delenv("MFP_READ_ONLY", raising=False)
    monkeypatch.delenv("MFP_WRITE_TOOLS", raising=False)
    monkeypatch.setattr(server, "_store", Store(tmp_path / "server.db"))
    monkeypatch.setattr(server.mfp_client, "get_client", lambda: library)
    monkeypatch.setattr(server.sync, "refresh_day", lambda store, client, day: None)
    return library


def test_server_logs_recipe_and_meal_by_id(connected):
    recipe = asyncio.run(
        server.fitness_log_food(recipe_id="pancakes-1", quantity=2, date="2026-07-08")
    )
    assert recipe["ok"] is True
    assert recipe["source"] == "my_recipe"
    assert recipe["quantity"] == 2

    meal = asyncio.run(
        server.fitness_log_food(
            saved_meal_id="meal-77", meal="lunch", quantity=1, date="2026-07-08"
        )
    )
    assert meal["ok"] is True
    assert meal["source"] == "my_meal"
    assert meal["meal"] == "lunch"
    assert len(meal["items"]) == 2


def test_server_search_reports_source(connected):
    found = asyncio.run(server.fitness_search_food(query="pancake"))
    same = asyncio.run(server.fitness_find_food(query="pancake"))
    assert found["results"][0]["source"] == "my_recipe"
    assert [(item["source"], item["name"]) for item in found["results"][:3]] == [
        (item["source"], item["name"]) for item in same["results"][:3]
    ]
    assert "warnings" not in found


class _Hang:
    status_code = 200
    text = ""

    def raise_for_status(self):
        raise TimeoutError("timed out")

    def json(self):
        raise TimeoutError("timed out")


def test_recent_timeout_keeps_other_sources(library):
    library.session.route("POST", "food/load_recent", _Hang())
    results, warnings = personal.find_food(library, "pancake")
    assert [item["source"] for item in results[:2]] == ["my_recipe", "frequent"]
    assert results[0]["name"] == "Homemade Pancakes"
    assert results[1]["name"] == "Pancake mix"
    assert warnings == [
        "Recent foods didn't respond (POST /food/load_recent); "
        "those results were left out."
    ]
    recent = [
        call
        for call in library.session.calls
        if call[0] == "POST" and "food/load_recent" in call[1]
    ]
    frequent = [
        call
        for call in library.session.calls
        if call[0] == "POST" and "food/load_most_used" in call[1]
    ]
    csrf = [
        call
        for call in library.session.calls
        if call[0] == "GET" and "food/add_to_diary" in call[1]
    ]
    assert recent[0][2]["timeout"] == personal._TAB_TIMEOUT
    assert frequent[0][2]["timeout"] == personal._TAB_TIMEOUT
    assert csrf[0][2]["timeout"] == personal._TAB_TIMEOUT


def test_hung_diary_page_is_not_fetched_again_for_frequent(library):
    library.session.route("GET", "food/add_to_diary", _Hang())
    recent, recent_warnings = personal.read_recent_foods(library)
    frequent, frequent_warnings = personal.read_frequent_foods(library)
    assert recent == []
    assert frequent == []
    assert recent_warnings[0].startswith("Recent foods didn't respond")
    assert "GET /food/add_to_diary" in recent_warnings[0]
    assert "GET /food/add_to_diary" in frequent_warnings[0]
    gets = [
        call
        for call in library.session.calls
        if call[0] == "GET" and "food/add_to_diary" in call[1]
    ]
    assert len(gets) == 1
    assert not any(
        call[0] == "POST" and "food/load_" in call[1] for call in library.session.calls
    )


def test_recent_auth_failure_still_raises(library, make_response):
    library.session.route(
        "POST", "food/load_recent", make_response(status_code=401, text="HTTP 401")
    )
    with pytest.raises(RuntimeError, match="401") as caught:
        personal.read_recent_foods(library)
    assert mfp_client.is_auth_error(caught.value) is True
    assert getattr(library, "_rails_csrf_failed", False) is False


def test_recipe_list_uses_embedded_nutrition_without_opening_the_view(
    library, make_response
):
    html = """
    <html><body><div id="main"><ul>
    <li data-protein="8"><div></div><div><h2><span>
    <a href="/recipe/view/pancakes-1" title="Homemade Pancakes">Homemade Pancakes</a>
    </span></h2></div></li>
    </ul></div>
    <script>
    MFP.Recipes.loadRecipe("pancakes-1", {"id":"pancakes-1","name":"Homemade Pancakes","servings":4,"nutritional_contents_per_serving":{"energy":{"value":240,"unit":"calories"},"protein":99,"carbohydrates":30,"fat":6}});
    </script>
    </body></html>
    """
    library.session.route("GET", "recipe_parser", make_response(text=html))
    listed = personal.list_my_recipes(library)
    assert listed[0]["calories"] == 240
    assert listed[0]["protein"] == 8
    assert listed[0]["carbs"] == 30
    assert listed[0]["fat"] == 6
    assert listed[0]["recipe_servings"] == 4
    assert not any(
        "recipe/view/" in url for _method, url, _payload in library.session.calls
    )


def test_recipe_logger_error_is_not_an_auth_error(library, make_response):
    library.session.route(
        "POST",
        "recipe/log_recipe",
        make_response(json_data={"status": "error"}),
    )
    with pytest.raises(personal.PersonalLookupError, match="could not log") as caught:
        personal.log_recipe(library, "pancakes-1", TODAY, "breakfast", 1)
    assert mfp_client.is_auth_error(caught.value) is False
    assert _food_add_posts(library) == []


def test_query_log_includes_a_warning_when_recent_times_out(library, store):
    library.session.route("POST", "food/load_recent", _Hang())
    result = food_logging.log_by_query(
        library, store, "pancakes", TODAY, "breakfast", 2
    )
    assert result["source"] == "my_recipe"
    assert result["recipe_id"] == "pancakes-1"
    assert "POST /food/load_recent" in result["warnings"][0]


def test_server_returns_warnings_instead_of_failing(connected):
    connected.session.route("POST", "food/load_recent", _Hang())
    found = asyncio.run(server.fitness_find_food(query="pancake"))
    assert found["results"][0]["source"] == "my_recipe"
    assert "POST /food/load_recent" in found["warnings"][0]
    listed = asyncio.run(server.fitness_list_recent_foods())
    assert listed["items"] == []
    assert "POST /food/load_recent" in listed["warnings"][0]
