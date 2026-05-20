"""smcity_fuzz — adversarial agent for driving the smcity smart-city agent.

v0.4.0 pipeline:

    synth(persona, dataset, language) ──▶ question
    runner(question) ──▶ (reply, tool_trace)          [POST /turn]
    judge(question, reply, trace, language) ──▶ rubric JSON
    store(row) ──▶ logs/fuzz_runs.jsonl
    report() ──▶ summarises failures by dataset / language / persona / reason

Both synth and judge run on a smaller model (gpt-oss-20b) from the same
LM Studio instance that hosts the production gpt-oss-120b brain — single
endpoint, zero new infra, ~5x faster per token than using 120b for
everything.
"""

__version__ = "0.4.17"
