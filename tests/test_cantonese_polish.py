"""Unit tests for the deterministic Cantonese polish pass."""
# ruff: noqa: RUF001

from __future__ import annotations

import pytest

from smcity.cantonese_polish import polish


@pytest.mark.parametrize(
    ("formal", "expected_snippet"),
    [
        # particle / copula
        ("我在上環", "我喺上環"),
        ("這是我的朋友", "係我嘅朋友"),
        ("他也可以", "佢都可以"),
        # negation
        ("我沒有時間", "我冇時間"),
        ("他不知道", "佢唔知"),
        # 3sg collapse
        ("她去了沙田", "佢去咗沙田"),
        # demonstrative
        ("這裡很熱", "呢度好熱"),
        ("那裡有籃球場", "嗰度有籃球場"),
        # pleasantries
        ("您好，我可以幫您嗎？", "你好"),
        ("不好意思，我沒有資料", "唔好意思"),
        ("謝謝", "多謝"),
        # plurals
        ("他們都知道", "佢哋都知"),
        # full sentence from the example set
        ("現在香港的溫度是 27 度，沒有下雨。", "而家香港嘅溫度係 27 度"),
    ],
)
def test_formal_to_cantonese_substitutions(formal: str, expected_snippet: str) -> None:
    out = polish(formal)
    assert expected_snippet in out, f"{formal!r} → {out!r} (missing {expected_snippet!r})"


def test_polish_preserves_already_cantonese_text() -> None:
    # An already-Cantonese sentence should come out (almost) unchanged.
    canto = "我而家喺上環，想搭 MTR 去沙田。你哋邊度最近？"
    out = polish(canto)
    # The only allowed drift is that particles that match our substitutions stay.
    assert out == canto


def test_polish_protects_taxi_word() -> None:
    # 的士 (taxi) must NOT become 嘅士.
    out = polish("搭的士去機場")
    assert "的士" in out
    assert "嘅士" not in out


def test_polish_maps_mandarin_now_to_cantonese_now() -> None:
    # 現在 → 而家 via phrase pass.
    out = polish("現在時間是 3 點")
    assert "而家" in out
    assert "現在" not in out


def test_polish_noop_on_non_chinese() -> None:
    out = polish("The next train is in 2 minutes.")
    assert out == "The next train is in 2 minutes."


def test_polish_noop_on_empty() -> None:
    assert polish("") == ""
    assert polish(None) is None  # type: ignore[arg-type]


def test_polish_does_not_break_fixed_phrases() -> None:
    # 正在 (progressive aspect) must not be rewritten to 正喺.
    out = polish("列車正在離開")
    assert "正在" in out or "正喺" not in out  # 正在→正在 (unchanged) is fine
    # 了解 must not become 咗解.
    out2 = polish("我了解這個情況")
    assert "咗解" not in out2


def test_polish_idempotent() -> None:
    once = polish("他不知道這個地方")
    twice = polish(once)
    assert once == twice
