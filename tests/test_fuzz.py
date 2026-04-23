# ruff: noqa: RUF001
"""Unit tests for smcity_fuzz — every upstream (fuzzer LM Studio + agent
/turn) is respx-mocked so CI stays hermetic."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from smcity_fuzz.datasets import TOPICS
from smcity_fuzz.datasets import by_id as topic_by_id
from smcity_fuzz.export import render_report
from smcity_fuzz.judge import JudgeError, JudgeVerdict, _system_prompt, judge
from smcity_fuzz.personas import PERSONAS
from smcity_fuzz.personas import by_id as persona_by_id
from smcity_fuzz.report import summarise
from smcity_fuzz.runner import run_campaign
from smcity_fuzz.settings import FuzzSettings
from smcity_fuzz.store import FuzzRow, append_row, read_rows
from smcity_fuzz.synth import SynthError, synthesise_question


@pytest.fixture
def tmp_settings(tmp_path: Path) -> FuzzSettings:
    """Per-test settings with a tmp JSONL path + fake endpoints."""
    return FuzzSettings(
        base_url="http://fuzz-llm.test/v1",
        model="openai/gpt-oss-20b",
        timeout_s=5.0,
        agent_url="http://agent.test",
        agent_timeout_s=5.0,
        runs_path=str(tmp_path / "fuzz_runs.jsonl"),
        concurrency=2,
    )


def _chat_completion_body(text: str) -> dict[str, Any]:
    return {
        "id": "c-1",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
    }


# --- synth --------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_synth_returns_clean_question(tmp_settings: FuzzSettings) -> None:
    respx.post("http://fuzz-llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_completion_body("下一班去中環嘅地鐵幾點到？"))
    )
    q = await synthesise_question(PERSONAS[0], TOPICS[0], "yue", settings=tmp_settings)
    assert q == "下一班去中環嘅地鐵幾點到？"


@pytest.mark.asyncio
@respx.mock
async def test_synth_strips_question_prefix(tmp_settings: FuzzSettings) -> None:
    respx.post("http://fuzz-llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json=_chat_completion_body('Question: "Where\'s the nearest public toilet?"')
        )
    )
    q = await synthesise_question(
        persona_by_id("english_tourist"),
        topic_by_id("public_toilets"),
        "en",
        settings=tmp_settings,
    )
    # 'Question:' + surrounding quotes stripped.
    assert q == "Where's the nearest public toilet?"


@pytest.mark.asyncio
@respx.mock
async def test_synth_raises_on_empty(tmp_settings: FuzzSettings) -> None:
    respx.post("http://fuzz-llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_completion_body("   "))
    )
    with pytest.raises(SynthError, match="empty content"):
        await synthesise_question(PERSONAS[0], TOPICS[0], "en", settings=tmp_settings)


@pytest.mark.asyncio
@respx.mock
async def test_synth_raises_on_http_error(tmp_settings: FuzzSettings) -> None:
    respx.post("http://fuzz-llm.test/v1/chat/completions").mock(return_value=httpx.Response(500))
    with pytest.raises(SynthError, match="HTTP"):
        await synthesise_question(PERSONAS[0], TOPICS[0], "en", settings=tmp_settings)


# --- judge --------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_judge_parses_clean_json(tmp_settings: FuzzSettings) -> None:
    verdict_json = {
        "intent_match": 2,
        "language_ok": 2,
        "tool_choice_ok": 2,
        "factual_vs_trace": 2,
        "coherence": 2,
        "failure_reasons": [],
        "summary": "Clean pass.",
    }
    respx.post("http://fuzz-llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_completion_body(json.dumps(verdict_json)))
    )
    v = await judge("下一班地鐵幾點？", "3 分鐘後", [], TOPICS[0], "yue", settings=tmp_settings)
    assert v.total_score == 10
    assert v.failed is False


@pytest.mark.asyncio
@respx.mock
async def test_judge_handles_markdown_fence(tmp_settings: FuzzSettings) -> None:
    body = (
        "```json\n"
        '{"intent_match": 1, "language_ok": 0, "tool_choice_ok": 1, '
        '"factual_vs_trace": 1, "coherence": 1, '
        '"failure_reasons": ["wrong_language"], "summary": "Answered in English."}\n'
        "```"
    )
    respx.post("http://fuzz-llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_completion_body(body))
    )
    v = await judge("搵個泳池", "Victoria Park", [], TOPICS[0], "yue", settings=tmp_settings)
    assert v.language_ok == 0
    assert "wrong_language" in v.failure_reasons
    assert v.failed is True


@pytest.mark.asyncio
@respx.mock
async def test_judge_raises_on_unparseable(tmp_settings: FuzzSettings) -> None:
    respx.post("http://fuzz-llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_completion_body("not json at all"))
    )
    with pytest.raises(JudgeError):
        await judge("q", "r", [], TOPICS[0], "en", settings=tmp_settings)


def test_judge_verdict_failed_on_low_score() -> None:
    v = JudgeVerdict(
        intent_match=2,
        language_ok=2,
        tool_choice_ok=0,  # ← triggers failed
        factual_vs_trace=2,
        coherence=2,
    )
    assert v.failed is True


# --- store --------------------------------------------------------------


def test_store_append_then_read(tmp_settings: FuzzSettings) -> None:
    row = FuzzRow(
        run_id="run-test",
        ts="2026-04-23T00:00:00+00:00",
        persona="cantonese_senior",
        language="yue",
        topic="mtr_next_trains",
        question="q",
        reply="r",
    )
    append_row(row, settings=tmp_settings)
    append_row(row.model_copy(update={"question": "q2"}), settings=tmp_settings)
    back = read_rows(settings=tmp_settings)
    assert len(back) == 2
    assert back[0].question == "q"
    assert back[1].question == "q2"


def test_store_skips_corrupt_line(tmp_settings: FuzzSettings) -> None:
    path = Path(tmp_settings.runs_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "not-json\n"
        + FuzzRow(
            run_id="run-test",
            ts="2026-04-23T00:00:00+00:00",
            persona="p",
            language="en",
            topic="t",
            question="q",
        ).model_dump_json()
        + "\n"
    )
    rows = read_rows(settings=tmp_settings)
    assert len(rows) == 1  # corrupt line dropped


# --- runner -------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_runner_end_to_end_happy_path(tmp_settings: FuzzSettings) -> None:
    # Synth → always the same question for determinism.
    respx.post("http://fuzz-llm.test/v1/chat/completions").mock(
        side_effect=[
            # synth call
            httpx.Response(200, json=_chat_completion_body("what's the MTR like?")),
            # judge call
            httpx.Response(
                200,
                json=_chat_completion_body(
                    json.dumps(
                        {
                            "intent_match": 2,
                            "language_ok": 2,
                            "tool_choice_ok": 2,
                            "factual_vs_trace": 2,
                            "coherence": 2,
                            "failure_reasons": [],
                            "summary": "pass",
                        }
                    )
                ),
            ),
        ]
    )
    respx.post("http://agent.test/turn").mock(
        return_value=httpx.Response(
            200,
            json={
                "session_id": "fuzz",
                "text": "Next train in 3 minutes.",
                "lang": {
                    "source": "detected",
                    "primary_lang": "en",
                    "upstream_langs_available": [],
                    "translation_applied": False,
                },
                "citations": [],
                "tool_trace": [
                    {
                        "index": 1,
                        "name": "transport.get_mtr_next_trains",
                        "args": {"station_name": "Central"},
                        "status": "ok",
                        "latency_ms": 200,
                        "result_summary": "2 trains",
                    }
                ],
                "followups": [],
                "elapsed_ms": 400,
            },
        )
    )
    run_id, rows = await run_campaign(
        personas=(persona_by_id("english_tourist"),),
        topics=(topic_by_id("mtr_next_trains"),),
        languages=("en",),
        settings=tmp_settings,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.run_id == run_id
    assert row.reply == "Next train in 3 minutes."
    assert row.judge is not None
    assert row.judge.total_score == 10
    assert row.failed is False
    persisted = read_rows(settings=tmp_settings)
    assert len(persisted) == 1


@pytest.mark.asyncio
@respx.mock
async def test_runner_records_agent_error(tmp_settings: FuzzSettings) -> None:
    respx.post("http://fuzz-llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat_completion_body("some q"))
    )
    respx.post("http://agent.test/turn").mock(return_value=httpx.Response(500))
    _, rows = await run_campaign(
        personas=(persona_by_id("english_tourist"),),
        topics=(topic_by_id("mtr_next_trains"),),
        languages=("en",),
        settings=tmp_settings,
    )
    assert len(rows) == 1
    assert rows[0].failed is True
    assert any(err.startswith("agent_http:") for err in rows[0].errors)


@pytest.mark.asyncio
@respx.mock
async def test_runner_records_synth_error(tmp_settings: FuzzSettings) -> None:
    respx.post("http://fuzz-llm.test/v1/chat/completions").mock(return_value=httpx.Response(500))
    _, rows = await run_campaign(
        personas=(persona_by_id("english_tourist"),),
        topics=(topic_by_id("mtr_next_trains"),),
        languages=("en",),
        settings=tmp_settings,
    )
    assert len(rows) == 1
    assert rows[0].failed is True
    assert any(err.startswith("synth:") for err in rows[0].errors)
    assert rows[0].reply is None


# --- report -------------------------------------------------------------


def test_judge_prompt_forbids_code_fixes() -> None:
    """The system prompt must explicitly keep the judge in diagnostic mode.

    This pins the guardrail so a well-meaning refactor can't accidentally
    let the judge start suggesting code patches.
    """
    prompt = _system_prompt(TOPICS[0], "en")
    lower = prompt.lower()
    assert "must not suggest" in lower or "not propose" in lower or "diagnostic only" in lower
    assert "code fix" in lower or "code" in lower
    # The prompt must still explain that a separate system (Claude/Gemini)
    # receives the diagnostic output for fixing.
    assert "claude" in lower or "gemini" in lower or "engineer" in lower


# --- export -------------------------------------------------------------


def _fail_row(topic: str = "swimming_pools") -> FuzzRow:
    return FuzzRow(
        run_id="run-export-test",
        ts="2026-04-23T12:00:00+00:00",
        persona="cantonese_senior",
        language="yue",
        topic=topic,
        question="搵個最近嘅泳池",
        reply="Victoria Park Swimming Pool",
        tool_trace=[
            {
                "index": 1,
                "name": "facility.find_nearby_pools",
                "args": {"district": "Wan Chai"},
                "status": "ok",
                "latency_ms": 2400,
                "result_summary": "3 pools",
            }
        ],
        elapsed_ms=3100,
        judge=JudgeVerdict(
            intent_match=2,
            language_ok=0,
            tool_choice_ok=2,
            factual_vs_trace=2,
            coherence=1,
            failure_reasons=["english_in_cantonese", "wrong_language"],
            summary="Replied in English despite Cantonese question.",
        ),
    )


def test_export_banner_forbids_code_suggestions() -> None:
    markdown = render_report([_fail_row()])
    lower = markdown.lower()
    assert "diagnostic" in lower
    assert "claude" in lower or "gemini" in lower
    assert "not propose" in lower or "not suggest" in lower or "does not propose" in lower


def test_export_includes_failure_section_and_raw_json() -> None:
    row = _fail_row()
    markdown = render_report([row])
    assert "## Failure 1" in markdown
    assert "cantonese_senior" in markdown
    assert "搵個最近嘅泳池" in markdown
    assert "Victoria Park Swimming Pool" in markdown
    assert "english_in_cantonese" in markdown
    assert "Replied in English despite Cantonese question." in markdown
    # Raw-row block for copy-paste into a fix session.
    assert "### Raw row (JSON)" in markdown
    assert '"run_id": "run-export-test"' in markdown


def test_export_only_failures_flag_excludes_passes() -> None:
    pass_row = FuzzRow(
        run_id="r",
        ts="t",
        persona="p",
        language="en",
        topic="mtr_next_trains",
        question="q",
        reply="ok",
        judge=JudgeVerdict(
            intent_match=2, language_ok=2, tool_choice_ok=2, factual_vs_trace=2, coherence=2
        ),
    )
    rows = [pass_row, _fail_row()]
    only = render_report(rows, only_failures=True)
    assert "Failure 1" in only
    assert only.count("## Failure") == 1
    all_rows = render_report(rows, only_failures=False)
    assert all_rows.count("## Failure") == 2


def test_export_max_failures_caps_sections() -> None:
    rows = [_fail_row(topic=f"topic_{i}") for i in range(5)]
    out = render_report(rows, only_failures=True, max_failures=2)
    assert out.count("## Failure") == 2


def test_summarise_counts_pass_and_fail(tmp_settings: FuzzSettings) -> None:
    pass_row = FuzzRow(
        run_id="r",
        ts="t",
        persona="p",
        language="en",
        topic="mtr_next_trains",
        question="q",
        reply="ok",
        judge=JudgeVerdict(
            intent_match=2, language_ok=2, tool_choice_ok=2, factual_vs_trace=2, coherence=2
        ),
    )
    fail_row = FuzzRow(
        run_id="r",
        ts="t",
        persona="p",
        language="yue",
        topic="swimming_pools",
        question="q",
        reply="r",
        judge=JudgeVerdict(
            intent_match=1,
            language_ok=0,
            tool_choice_ok=1,
            factual_vs_trace=1,
            coherence=1,
            failure_reasons=["wrong_language"],
            summary="English when yue expected.",
        ),
    )
    text = summarise([pass_row, fail_row])
    assert "total: 2" in text
    assert "passed: 1" in text
    assert "failed: 1" in text
    assert "wrong_language" in text
    assert "swimming_pools" in text
