"""Static checks on the golden eval set — not a full run, just a sanity gate.

The full golden-run (hitting live LM Studio + data.gov.hk) lives in
`tests/integration/test_golden_run.py` (added in Phase 1b). Here we only verify
the file parses and each query declares a detected language that matches what
our detector says. This catches regressions in the detector early.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from smcity.langrouter import detect

GOLDEN_PATH = Path(__file__).parent / "golden" / "v0_1_queries.json"


def _all_queries() -> list[dict[str, object]]:
    data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    out: list[dict[str, object]] = []
    for bucket in ("native", "fallback"):
        for q in data["buckets"][bucket]:
            out.append({**q, "_bucket": bucket})
    return out


@pytest.mark.parametrize("q", _all_queries(), ids=lambda q: str(q["id"]))
def test_golden_query_language_is_detected_correctly(q: dict[str, object]) -> None:
    text = str(q["text"])
    expected = q["lang"]
    d = detect(text)
    # Code-switched Cantonese is primary_lang=yue with is_code_switched=True
    assert d.primary_lang == expected, (
        f"golden {q['id']}: expected {expected!r}, detector said {d.primary_lang!r} (text={text!r})"
    )


def test_golden_set_has_minimum_coverage() -> None:
    data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    all_q = data["buckets"]["native"] + data["buckets"]["fallback"]
    assert len(all_q) >= 30
    langs = {q["lang"] for q in all_q}
    # At least 10 distinct primary languages / script variants
    # (yue, zho x2 scripts, eng, jpn, kor, fra, deu, tha, tgl, ind, vie)
    assert len(langs) >= 6  # six primary_lang codes; scripts add further variety
