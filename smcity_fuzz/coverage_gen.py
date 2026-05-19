"""Coverage-test question generator.

Uses a small/medium synth model (gemma-3-12b by default — gemma-3-27b
won't fit alongside gpt-oss-120b in 96 GB unified memory) to generate
*stratified* English questions covering every dataset in the smcity
coverage catalog. Each dataset gets roughly count/N questions; OSM POI
categories share a generation pool keyed on the OSM category name.

The output is a JSONL file where each line is one question, tagged with
the dataset it was generated FOR (`expected_dataset_id`) and the tools
the agent SHOULD invoke when answering it (`expected_tools`). The runner
then drives them through /turn; the analyzer checks whether the agent's
tool trace matched expectation.

Generation is done in batches (default 25 questions per Gemma call) so
the per-question latency is amortised.

Usage::

    python -m smcity_fuzz coverage generate \\
        --count 10000 \\
        --gemma-model gemma-synth \\
        --lm-base-url http://host.docker.internal:1234/v1 \\
        --out logs/coverage_questions_v1.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Bundled catalog — same one served at /coverage. Path-resolved relative
# to this file so the harness works in dev (running from the repo root)
# and in the production container.
_CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "coverage_catalog.json"

_DEFAULT_BATCH = 25
_DEFAULT_TIMEOUT = 60.0


@dataclass(slots=True, frozen=True)
class GenerationTarget:
    """One stratum of generation — a (dataset_id, prompt-context) pair."""

    dataset_id: str
    title: str
    category: str
    description_for_prompt: str
    expected_tools: tuple[str, ...]


def _load_catalog() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    return data


def _build_targets(catalog: dict[str, Any]) -> list[GenerationTarget]:
    """Turn the catalog into a flat list of generation strata.

    Datasets in the xlsx → one target each. OSM POI categories collapse
    naturally because they all wire to the same tool (geo.search_osm_pois)
    but the LLM should still produce category-flavoured questions.
    """
    targets: list[GenerationTarget] = []
    for entry in catalog["datasets"]:
        desc = _describe_for_prompt(entry)
        targets.append(
            GenerationTarget(
                dataset_id=entry["id"],
                title=entry["title"],
                category=entry["category"],
                description_for_prompt=desc,
                expected_tools=tuple(entry.get("tools") or ()),
            )
        )
    # Extend with additional integrations that aren't in the xlsx but are
    # wired (weather, AQHI, LCSD, housing, real-time bus operators…).
    for entry in catalog.get("additional_integrations", []):
        if not entry.get("tools"):
            continue
        targets.append(
            GenerationTarget(
                dataset_id=f"X-{entry['title'][:20].replace(' ', '_')}",
                title=entry["title"],
                category=entry["category"],
                description_for_prompt=_describe_extra_for_prompt(entry),
                expected_tools=tuple(entry["tools"]),
            )
        )
    return targets


def _describe_for_prompt(entry: dict[str, Any]) -> str:
    """One-paragraph description suitable for a Gemma prompt."""
    osm = entry.get("osm_category")
    if osm:
        # OSM POI categories — make the question style explicit.
        return (
            f"Locating Hong Kong **{entry['title'].lower()}** "
            f"(OSM category `{osm}`). Typical user need: 'where is the nearest …', "
            f"'find … in <district/neighbourhood>', 'list … around <MTR station>'."
        )
    return (
        f"**{entry['title']}** ({entry['category']}). "
        f"Source: {entry['source']}. "
        f"{entry.get('notes', '')}"
    ).strip()


def _describe_extra_for_prompt(entry: dict[str, Any]) -> str:
    return (
        f"**{entry['title']}** ({entry['category']}). "
        f"Source: {entry['source']}. "
        f"{entry.get('notes', '')}"
    ).strip()


_SYSTEM_PROMPT = (
    "You are a question generator for a Hong Kong smart-city assistant. "
    "Generate DIVERSE, REALISTIC English questions that a Hong Kong resident "
    "or visitor might ask. The questions will be sent to an agent that has "
    "access to live HK government data + OpenStreetMap. "
    "\n\nStyle requirements:"
    "\n- Natural conversational English."
    "\n- Mix question forms (where, how do I, when, what time, is there, can you …)."
    "\n- Include SPECIFIC HK places: districts, MTR stations, malls, neighbourhoods, "
    "streets, landmarks, universities, parks."
    "\n- Vary phrasing — no two questions should be near-rewordings of each other."
    "\n- Each question must be answerable using ONLY the dataset described in the prompt."
    "\n- Single sentence, ≤ 25 words."
    "\n\nReturn ONLY a JSON array of question strings. No preamble, no commentary, "
    "no markdown code fence. Just the array."
)


def _user_prompt(target: GenerationTarget, batch_size: int) -> str:
    return (
        f"Dataset: {target.description_for_prompt}\n\n"
        f"Generate {batch_size} distinct English questions about THIS dataset."
    )


async def _gemma_complete(
    client: httpx.AsyncClient,
    *,
    lm_base_url: str,
    model: str,
    target: GenerationTarget,
    batch_size: int,
    temperature: float = 0.95,
) -> list[str]:
    """One Gemma call. Returns the parsed list of question strings, or
    an empty list on failure (logged)."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _user_prompt(target, batch_size)},
        ],
        "temperature": temperature,
        "max_tokens": 1500,
    }
    try:
        r = await client.post(f"{lm_base_url}/chat/completions", json=payload)
        r.raise_for_status()
        body = r.json()
    except httpx.HTTPError as err:
        log.warning("gemma_call_failed", extra={"dataset": target.dataset_id, "err": str(err)})
        return []
    text = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return _parse_question_list(text)


def _parse_question_list(text: str) -> list[str]:
    """Extract a JSON array of strings from Gemma's reply. Tolerates code
    fences and trailing prose by isolating the outermost [...]."""
    if not text:
        return []
    # Strip a code fence if present.
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    # Locate the outermost [...] block.
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        arr = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    return [str(x).strip() for x in arr if isinstance(x, str) and str(x).strip()]


async def generate(
    *,
    count: int,
    gemma_model: str,
    lm_base_url: str,
    output_path: Path,
    batch_size: int = _DEFAULT_BATCH,
    concurrency: int = 2,
) -> int:
    """Generate `count` questions stratified across the catalog, append-write
    each as a JSONL row to `output_path`. Returns the number of questions
    actually written (may be less than `count` if Gemma failures occur)."""
    catalog = _load_catalog()
    targets = _build_targets(catalog)
    per_target = max(1, count // len(targets))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    seen: set[str] = set()
    written = 0
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        async def _gen_one_batch(target: GenerationTarget, n: int) -> list[str]:
            async with sem:
                return await _gemma_complete(
                    client,
                    lm_base_url=lm_base_url,
                    model=gemma_model,
                    target=target,
                    batch_size=n,
                )

        tasks = []
        for target in targets:
            n_batches = (per_target + batch_size - 1) // batch_size
            for _ in range(n_batches):
                tasks.append((target, _gen_one_batch(target, batch_size)))

        with output_path.open("a", encoding="utf-8") as fh:
            for target, coro in tasks:
                qs = await coro
                for q in qs:
                    key = q.strip().casefold()
                    if key in seen:
                        continue
                    seen.add(key)
                    row = {
                        "id": str(uuid.uuid4()),
                        "expected_dataset_id": target.dataset_id,
                        "expected_dataset_title": target.title,
                        "expected_dataset_category": target.category,
                        "expected_tools": list(target.expected_tools),
                        "question_en": q,
                        "language": "en",
                        "generated_by": gemma_model,
                        "generated_at": datetime.now(UTC).isoformat(),
                    }
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    written += 1
                    if written >= count:
                        return written
                # Progress to stderr so the operator can watch.
                print(
                    f"\r[coverage_gen] {written}/{count} questions "
                    f"(seen={len(seen)}, last={target.dataset_id})       ",
                    end="",
                    file=sys.stderr,
                    flush=True,
                )
        print(file=sys.stderr)
    return written


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smcity_fuzz coverage generate")
    parser.add_argument("--count", type=int, default=10000)
    parser.add_argument("--gemma-model", default="gemma-synth")
    parser.add_argument(
        "--lm-base-url",
        default="http://host.docker.internal:1234/v1",
        help="LM Studio OpenAI-compatible base URL.",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=_DEFAULT_BATCH)
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args(list(argv) if argv is not None else None)
    written = asyncio.run(
        generate(
            count=args.count,
            gemma_model=args.gemma_model,
            lm_base_url=args.lm_base_url,
            output_path=args.out,
            batch_size=args.batch_size,
            concurrency=args.concurrency,
        )
    )
    print(f"wrote {written} questions to {args.out}", file=sys.stderr)
    return 0 if written > 0 else 1


__all__ = ["GenerationTarget", "generate", "main"]


if __name__ == "__main__":
    sys.exit(main())
