"""WebSocket origin allow-list tests (P1-1)."""

from __future__ import annotations

import pytest

from smcity.app import _allowed_origin
from smcity.session import is_valid_session_id
from smcity.settings import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


def test_same_origin_allowed_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WS_ALLOWED_ORIGINS", "")
    get_settings.cache_clear()
    assert _allowed_origin("http://127.0.0.1:8080", "127.0.0.1:8080") is True


def test_cross_origin_rejected_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WS_ALLOWED_ORIGINS", "")
    get_settings.cache_clear()
    assert _allowed_origin("http://evil.example", "127.0.0.1:8080") is False


def test_missing_origin_is_allowed_tailnet_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WS_ALLOWED_ORIGINS", "")
    get_settings.cache_clear()
    # curl / wscat without an Origin header — allowed in the tailnet posture.
    assert _allowed_origin("", "127.0.0.1:8080") is True


def test_wildcard_allows_any_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WS_ALLOWED_ORIGINS", "*")
    get_settings.cache_clear()
    assert _allowed_origin("http://anywhere.example", "127.0.0.1:8080") is True


def test_allowlist_host_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WS_ALLOWED_ORIGINS", "studio.example:8080")
    get_settings.cache_clear()
    assert _allowed_origin("http://studio.example:8080", "127.0.0.1:8080") is True
    assert _allowed_origin("http://other.example:8080", "127.0.0.1:8080") is False


def test_allowlist_full_scheme_host_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WS_ALLOWED_ORIGINS", "https://robot.example")
    get_settings.cache_clear()
    # host_header deliberately differs so the same-origin fallback doesn't satisfy the check.
    assert _allowed_origin("https://robot.example", "127.0.0.1:8080") is True
    # same host but wrong scheme (plain http) is rejected
    assert _allowed_origin("http://robot.example", "127.0.0.1:8080") is False


def test_session_id_validation() -> None:
    assert is_valid_session_id("abc123")
    assert is_valid_session_id("a_b.c-1")
    assert is_valid_session_id("A" * 64)
    assert not is_valid_session_id("")
    assert not is_valid_session_id("A" * 65)
    assert not is_valid_session_id("has space")
    assert not is_valid_session_id("has/slash")
    assert not is_valid_session_id("../etc/passwd")
    assert not is_valid_session_id("utf8é")
