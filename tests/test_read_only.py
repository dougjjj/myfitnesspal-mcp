import asyncio
import inspect

import pytest

from myfitnesspal_mcp import config, server
from myfitnesspal_mcp.store import Store


def _call(name: str, kwargs: dict):
    fn = getattr(server, name)
    if inspect.iscoroutinefunction(fn):
        return asyncio.run(fn(**kwargs))
    return fn(**kwargs)


WRITE_CALLS = {
    "fitness_draft_food": {"query": "banana"},
    "fitness_log_food": {"query": "banana"},
    "fitness_clear_food_pin": {"query": "banana"},
    "fitness_delete_food": {"query": "banana"},
    "fitness_modify_food": {"query": "banana"},
    "fitness_log_weight": {"weight": 70},
    "fitness_log_water": {"amount": 1},
    "fitness_delete_exercise": {"query": "run"},
    "fitness_log_note": {"text": "note"},
    "fitness_log_feel": {"note": "tired"},
}


@pytest.fixture(autouse=True)
def _read_only_env(monkeypatch):
    monkeypatch.delenv("MFP_ALLOW_WRITES", raising=False)
    monkeypatch.delenv("MFP_READ_ONLY", raising=False)
    monkeypatch.delenv("MFP_WRITE_TOOLS", raising=False)


def test_every_registered_tool_is_classified():
    registered = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    assert registered == server.READ_TOOLS | server.WRITE_TOOLS
    assert server.READ_TOOLS.isdisjoint(server.WRITE_TOOLS)
    for tool in server.mcp._tool_manager.list_tools():
        marked = getattr(tool.fn, "__mfp_write_tool__", False)
        if tool.name in server.WRITE_TOOLS:
            assert marked, tool.name
            assert tool.annotations.readOnlyHint is False
        else:
            assert not marked, tool.name
            assert tool.annotations.readOnlyHint is True
        assert (
            "query"
            in server.mcp._tool_manager.get_tool("fitness_log_food").parameters[
                "properties"
            ]
        )


def test_writes_are_off_by_default():
    assert config.writes_allowed() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
def test_allow_writes_opt_in(monkeypatch, value):
    monkeypatch.setenv("MFP_ALLOW_WRITES", value)
    assert config.writes_allowed() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_other_allow_writes_values_stay_read_only(monkeypatch, value):
    monkeypatch.setenv("MFP_ALLOW_WRITES", value)
    assert config.writes_allowed() is False


def test_read_only_env_wins_over_allow_writes(monkeypatch):
    monkeypatch.setenv("MFP_ALLOW_WRITES", "1")
    monkeypatch.setenv("MFP_READ_ONLY", "1")
    assert config.writes_allowed() is False


def test_write_tools_raise_before_any_client_or_store_use(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("write tool ran past the read-only gate")

    monkeypatch.setattr(server.mfp_client, "get_client", boom)
    monkeypatch.setattr(server, "get_store", boom)

    assert set(WRITE_CALLS) == set(server.WRITE_TOOLS)
    for name, kwargs in WRITE_CALLS.items():
        with pytest.raises(server.ReadOnlyError, match="MFP_ALLOW_WRITES"):
            _call(name, kwargs)


def test_read_only_env_blocks_even_when_writes_are_opted_in(monkeypatch):
    monkeypatch.setenv("MFP_ALLOW_WRITES", "1")
    monkeypatch.setenv("MFP_READ_ONLY", "1")
    with pytest.raises(server.ReadOnlyError):
        server.fitness_log_feel(note="nope")


def test_read_tools_still_run(tmp_path, monkeypatch):
    store = Store(tmp_path / "cache.db")
    monkeypatch.setattr(server, "_store", store)
    store.upsert_nutrition("2026-07-05", calories=1500.0)

    exported = asyncio.run(
        server.fitness_bulk_export(start="2026-07-01", end="2026-07-08")
    )
    assert exported["count"] == 1
    assert server.fitness_list_food_pins() == {"pins": []}


def test_opt_in_allows_a_local_write(tmp_path, monkeypatch):
    monkeypatch.setenv("MFP_ALLOW_WRITES", "1")
    store = Store(tmp_path / "cache.db")
    monkeypatch.setattr(server, "_store", store)
    result = server.fitness_log_feel(note="strong", rating=4, date="2026-07-08")
    assert result["note"] == "strong"
    assert store.feel("2026-07-08")["rating"] == 4


def _visible_names():
    tools = asyncio.run(server.mcp.list_tools())
    return {tool.name for tool in tools}


def test_hidden_writes_are_not_listed_by_default():
    assert _visible_names() == set(server.READ_TOOLS)


def test_unset_allowlist_lists_every_write_tool(monkeypatch):
    monkeypatch.setenv("MFP_ALLOW_WRITES", "1")
    assert _visible_names() == set(server.READ_TOOLS | server.WRITE_TOOLS)


def test_food_alias_enables_only_diary_writes(monkeypatch):
    monkeypatch.setenv("MFP_ALLOW_WRITES", "1")
    monkeypatch.setenv("MFP_WRITE_TOOLS", " food ")
    assert server.enabled_write_tools() == server.FOOD_WRITE_TOOLS
    assert _visible_names() == set(server.READ_TOOLS | server.FOOD_WRITE_TOOLS)
    assert "fitness_search_food" in _visible_names()
    for blocked in (
        "fitness_draft_food",
        "fitness_clear_food_pin",
        "fitness_log_feel",
        "fitness_log_water",
        "fitness_log_weight",
        "fitness_log_note",
        "fitness_delete_exercise",
    ):
        assert blocked not in _visible_names()


def test_food_alias_blocks_other_writes_before_side_effects(monkeypatch):
    monkeypatch.setenv("MFP_ALLOW_WRITES", "1")
    monkeypatch.setenv("MFP_WRITE_TOOLS", "food")

    def boom(*args, **kwargs):
        raise AssertionError("blocked write tool ran past the allowlist")

    monkeypatch.setattr(server.mfp_client, "get_client", boom)
    monkeypatch.setattr(server, "get_store", boom)

    for name in server.WRITE_TOOLS - server.FOOD_WRITE_TOOLS:
        with pytest.raises(server.ReadOnlyError, match="MFP_WRITE_TOOLS"):
            _call(name, WRITE_CALLS[name])
        with pytest.raises(server.ToolError, match="Unknown tool"):
            asyncio.run(server.mcp.call_tool(name, {}))

    for name in server.FOOD_WRITE_TOOLS:
        with pytest.raises(AssertionError, match="past the allowlist"):
            _call(name, WRITE_CALLS[name])


def test_explicit_names_and_food_can_be_combined(monkeypatch):
    monkeypatch.setenv("MFP_ALLOW_WRITES", "1")
    monkeypatch.setenv("MFP_WRITE_TOOLS", "food, fitness_log_water")
    assert server.enabled_write_tools() == server.FOOD_WRITE_TOOLS | {
        "fitness_log_water"
    }


def test_write_tools_without_allow_writes_does_not_enable(monkeypatch):
    monkeypatch.setenv("MFP_WRITE_TOOLS", "food")
    assert server.enabled_write_tools() == frozenset()
    assert _visible_names() == set(server.READ_TOOLS)


def test_read_only_overrides_food_allowlist(monkeypatch):
    monkeypatch.setenv("MFP_ALLOW_WRITES", "1")
    monkeypatch.setenv("MFP_WRITE_TOOLS", "food")
    monkeypatch.setenv("MFP_READ_ONLY", "1")
    assert server.enabled_write_tools() == frozenset()
    with pytest.raises(server.ReadOnlyError, match="MFP_ALLOW_WRITES"):
        _call("fitness_log_food", WRITE_CALLS["fitness_log_food"])
    assert "fitness_log_food" not in _visible_names()


def test_unknown_write_tool_name_is_rejected(monkeypatch):
    monkeypatch.setenv("MFP_ALLOW_WRITES", "1")
    monkeypatch.setenv("MFP_WRITE_TOOLS", "fitness_log_foods")
    with pytest.raises(server.UnknownWriteToolsError, match="fitness_log_foods"):
        server.enabled_write_tools()


def test_cli_rejects_unknown_write_tool(monkeypatch):
    from myfitnesspal_mcp import cli

    monkeypatch.setenv("MFP_ALLOW_WRITES", "1")
    monkeypatch.setenv("MFP_WRITE_TOOLS", "nope")
    monkeypatch.setattr("sys.argv", ["mfp-mcp"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 2
