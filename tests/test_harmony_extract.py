"""Unit tests for the harmony-format tool-call leak extractor."""
# ruff: noqa: RUF001  # CJK fullwidth punctuation is intentional in the sample.

from __future__ import annotations

from smcity.llm import extract_harmony_tool_calls


def test_extracts_meta_ask_user_leaked_as_harmony() -> None:
    leaked = (
        "<|start|>assistant<|channel|>commentary to=functions.meta_ask_user "
        "<|constrain|>json<|message|>"
        '{"question":"請問您的目的站是哪一站？","slot":"destination"}'
    )
    text, calls = extract_harmony_tool_calls(
        leaked, known_tool_names={"meta.ask_user", "transport.plan_simple_route"}
    )
    assert text == ""  # Leak consumed entirely.
    assert len(calls) == 1
    assert calls[0]["name"] == "meta.ask_user"
    assert "question" in calls[0]["arguments"]
    assert "destination" in calls[0]["arguments"]


def test_extractor_handles_dotted_name_directly() -> None:
    leaked = (
        "<|start|>assistant<|channel|>commentary to=functions.meta.ask_user "
        '<|constrain|>json<|message|>{"question":"which mode?","slot":"mode"}'
    )
    _, calls = extract_harmony_tool_calls(leaked)
    assert calls[0]["name"] == "meta.ask_user"


def test_extractor_strips_stray_harmony_tokens() -> None:
    leaked = "Hello <|return|> world <|end|>"
    text, calls = extract_harmony_tool_calls(leaked)
    assert calls == []
    assert "<|" not in text
    assert "Hello" in text and "world" in text


def test_extractor_noop_on_clean_text() -> None:
    text, calls = extract_harmony_tool_calls("Plain English reply with no tokens.")
    assert calls == []
    assert text == "Plain English reply with no tokens."


def test_extractor_maps_transport_plan_simple_route() -> None:
    leaked = (
        "<|start|>assistant<|channel|>commentary "
        "to=functions.transport_plan_simple_route "
        "<|constrain|>json<|message|>"
        '{"origin_station":"MOK","destination_station":"CEN"}'
    )
    _, calls = extract_harmony_tool_calls(
        leaked,
        known_tool_names={"transport.plan_simple_route", "meta.ask_user"},
    )
    assert calls[0]["name"] == "transport.plan_simple_route"
    assert '"origin_station":"MOK"' in calls[0]["arguments"]


def test_extractor_handles_multiple_leaks() -> None:
    leaked = (
        "<|start|>assistant<|channel|>commentary to=functions.meta_ask_user "
        '<|constrain|>json<|message|>{"question":"q1","slot":"mode"}'
        "<|start|>assistant<|channel|>commentary to=functions.meta_ask_user "
        '<|constrain|>json<|message|>{"question":"q2","slot":"destination"}'
    )
    _, calls = extract_harmony_tool_calls(leaked, known_tool_names={"meta.ask_user"})
    assert len(calls) == 2
    assert '"q1"' in calls[0]["arguments"]
    assert '"q2"' in calls[1]["arguments"]
