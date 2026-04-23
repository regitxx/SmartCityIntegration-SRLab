"""Phase 2c-fix: bare-leak harmony extractor + source-footer rewriter tests."""
# ruff: noqa: RUF001

from __future__ import annotations

from smcity.llm import extract_harmony_tool_calls
from smcity.orchestrator import _rewrite_source_footer
from smcity.schemas import Citation


def test_bare_leak_transport_plan_simple_route() -> None:
    leaked = (
        'transport_plan_simple_route json{"origin":"Mong Kok, Hong Kong",'
        '"destination":"Holy Cross Church, St Francis Street, Mong Kok, '
        'Hong Kong","mode":"walk"}'
    )
    text, calls = extract_harmony_tool_calls(
        leaked,
        known_tool_names={
            "transport.plan_simple_route",
            "transport.find_stops_by_name",
            "meta.ask_user",
        },
    )
    assert calls, "expected the bare leak to be recovered"
    assert calls[0]["name"] == "transport.plan_simple_route"
    assert '"mode":"walk"' in calls[0]["arguments"]
    assert "transport_plan_simple_route" not in text  # stripped


def test_bare_leak_recovers_dotted_tool_name_form() -> None:
    """v0.4.7 regression: gpt-oss-120b in live runs emits the dotted form
    (`transport.plan_journey json{...}`) which the underscored-only pattern
    let through. Verify both forms are now stripped + recovered."""
    leaked = 'transport.plan_journey json{"origin":"尖沙咀","destination":"中環"}'
    text, calls = extract_harmony_tool_calls(
        leaked,
        known_tool_names={
            "transport.plan_journey",
            "transport.plan_simple_route",
            "meta.ask_user",
        },
    )
    assert calls, "dotted-form leak must now be caught"
    assert calls[0]["name"] == "transport.plan_journey"
    assert "transport.plan_journey" not in text  # stripped from reply


def test_bare_leak_ignored_without_known_names() -> None:
    # Prose-style text mentioning a fake tool should NOT be mis-extracted.
    text, calls = extract_harmony_tool_calls(
        'Take the transport_plan_simple_route json{"foo":"bar"} there.'
    )
    assert calls == []  # gated by known_tool_names
    assert "transport_plan_simple_route" in text


def test_bare_leak_skipped_when_name_not_in_registry() -> None:
    leaked = 'made_up_tool json{"foo":"bar"}'
    _, calls = extract_harmony_tool_calls(leaked, known_tool_names={"real.tool_name"})
    assert calls == []


def test_source_footer_strips_hallucinated_and_appends_real() -> None:
    reply = "Take the MTR eastbound.\n\nsrc: mtr_next_trains / lcsd_courts"
    out = _rewrite_source_footer(
        reply,
        [
            Citation(
                tool="transport.get_mtr_next_trains",
                upstream="rt.data.gov.hk/mtr",
                fetched_at="2026-04-21T19:52:00Z",  # type: ignore[arg-type]
            )
        ],
    )
    # Hallucinated 'lcsd_courts' must be gone.
    assert "lcsd_courts" not in out
    # Real tool must appear.
    assert "src: get_mtr_next_trains" in out
    # Main body preserved.
    assert "Take the MTR eastbound." in out


def test_source_footer_dedupes_and_preserves_order() -> None:
    reply = "A reply."
    citations = [
        Citation(
            tool="transport.get_mtr_next_trains",
            upstream="rt.data.gov.hk/mtr",
            fetched_at="2026-04-21T19:52:00Z",  # type: ignore[arg-type]
        ),
        Citation(
            tool="transport.get_mtr_next_trains",  # dup
            upstream="rt.data.gov.hk/mtr",
            fetched_at="2026-04-21T19:52:00Z",  # type: ignore[arg-type]
        ),
        Citation(
            tool="context.get_current_weather",
            upstream="data.weather.gov.hk",
            fetched_at="2026-04-21T19:52:00Z",  # type: ignore[arg-type]
        ),
    ]
    out = _rewrite_source_footer(reply, citations)
    # Order: first occurrence wins, duplicates removed.
    assert out.endswith("src: get_mtr_next_trains / get_current_weather")


def test_source_footer_with_no_citations_strips_any_fake_src() -> None:
    reply = "Hi.\nsrc: pretend_tool"
    out = _rewrite_source_footer(reply, [])
    assert "src:" not in out
    assert "pretend_tool" not in out
    assert "Hi." in out


def test_source_footer_handles_fullwidth_colon() -> None:
    reply = "Answer.\nsrc： fake_tool / other_fake"
    out = _rewrite_source_footer(reply, [])
    assert "fake_tool" not in out
    assert "Answer." in out
