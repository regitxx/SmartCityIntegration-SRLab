"""Script normalisation — HK-aware Simplified ↔ Traditional conversion.

Uses OpenCC's s2hk/hk2s conversion maps. Critical: we use `s2hk.json` (Simp →
HK Traditional), never `s2t.json` (Simp → Taiwan Traditional), because the
latter silently rewrites Cantonese-specific characters.
"""

from __future__ import annotations

from functools import cache
from typing import Any, Protocol

try:
    from opencc import OpenCC as _OpenCC  # type: ignore[import-untyped]
except Exception:  # pragma: no cover - optional dep at import time
    _OpenCC = None


class _Converter(Protocol):
    def convert(self, text: str) -> str: ...


@cache
def _s2hk() -> _Converter | None:
    if _OpenCC is None:
        return None
    inst: Any = _OpenCC("s2hk")
    return inst  # type: ignore[no-any-return]


@cache
def _hk2s() -> _Converter | None:
    if _OpenCC is None:
        return None
    inst: Any = _OpenCC("hk2s")
    return inst  # type: ignore[no-any-return]


def simplified_to_hk(text: str) -> str:
    """Simplified Chinese → Hong Kong Traditional.

    No-ops if OpenCC is unavailable — the caller keeps the raw text.
    """
    conv = _s2hk()
    if conv is None:
        return text
    return conv.convert(text)


def hk_to_simplified(text: str) -> str:
    """Hong Kong Traditional → Simplified Chinese."""
    conv = _hk2s()
    if conv is None:
        return text
    return conv.convert(text)
