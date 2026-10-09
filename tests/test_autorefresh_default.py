import pytest

from myfitnesspal_mcp import refresh


@pytest.fixture(autouse=True)
def _clear_autorefresh(monkeypatch):
    monkeypatch.delenv("MFP_AUTOREFRESH", raising=False)


def test_autorefresh_is_off_even_when_playwright_imports(monkeypatch):
    monkeypatch.setattr(refresh, "playwright_installed", lambda: True)
    assert refresh.available() is False
    monkeypatch.setenv("MFP_AUTOREFRESH", "1")
    assert refresh.available() is True


def test_refresh_does_not_launch_a_browser_unless_enabled(monkeypatch):
    monkeypatch.setattr(refresh, "playwright_installed", lambda: True)
    monkeypatch.setattr(refresh, "profile_seeded", lambda: True)
    launched = []
    monkeypatch.setattr(
        refresh, "_visit_and_harvest", lambda seed: launched.append(seed) or {}
    )
    monkeypatch.setattr(refresh.mfp_client, "reset", lambda: None)

    refresh.refresh_session()

    assert launched == []
