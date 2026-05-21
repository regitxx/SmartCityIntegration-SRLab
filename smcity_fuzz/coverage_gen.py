"""Coverage-test question generator.

Uses a small synth model (Gemma 4 by default — `gemma-synth` identifier
in LM Studio) to generate *stratified, multilingual* questions covering
every dataset in the smcity coverage catalog.

Stratified means:
- per-dataset: roughly count/N questions for each of the N catalog rows
- per-language: each (dataset, lang) cell gets count/(N*L) questions,
  for L = 4 langs by default — en, yue, zh-Hant, zh-Hans

Each output row is tagged with the dataset it was generated FOR
(`expected_dataset_id`) and the language it was emitted in (`language`).
The runner drives them through /turn; the analyzer in
`smcity_fuzz.contracts.evaluate` decides if the agent did the right thing.

Usage::

    python -m smcity_fuzz coverage generate \\
        --count 10000 \\
        --gemma-model gemma-synth \\
        --languages en,yue,zh-Hant,zh-Hans \\
        --lm-base-url http://host.docker.internal:1234/v1 \\
        --out logs/coverage_questions_v2.jsonl
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


# Per-language register guidance. Cantonese is colloquial HK written form
# (LIHKG / WhatsApp), zh-Hant is formal traditional Chinese, zh-Hans is
# Mandarin in simplified characters, en is conversational HK English.
_LANG_STYLE: dict[str, str] = {
    "en": (
        "Natural conversational English as a Hong Kong resident or visitor "
        "would type it. Mix question forms (where, how do I, when, what time, "
        "is there, can you, find me)."
    ),
    "yue": (
        "Natural written Cantonese (粵語 / 廣東話) — the way a Hong Konger "
        "would type in WhatsApp or LIHKG. Use 嘅 / 喺 / 咗 / 冇 / 佢 / 唔 / "
        "係 / 嗰 / 啲 / 咁 / 點 / 而家 instead of 的 / 在 / 了 / 沒 / 他 / "
        "不 / 是 / 那 / 些 / 這樣 / 怎 / 現在. NEVER use formal book-Mandarin."
    ),
    "zh-Hant": (
        "Formal Traditional Chinese (繁體中文), the standard written form. "
        "Polite, neutral tone — what a news article or a formal email would use."
    ),
    "zh-Hans": (
        "Simplified Chinese (简体中文), Mandarin written form, neutral register."
    ),
}

# Reference few-shot questions per language and topic family, so Gemma
# locks on to the right register on the first batch.
_FEW_SHOT: dict[str, list[str]] = {
    "en": [
        "where is the nearest convenience store to PolyU?",
        "how do I get from Mong Kok to Central?",
        "what time is the next train at Wan Chai?",
        "is there a public toilet near Festival Walk?",
    ],
    "yue": [
        "PolyU 附近最近嘅便利店喺邊?",
        "由旺角去中環點搭好?",
        "灣仔站下一班車幾時到?",
        "又一城附近有冇公廁?",
    ],
    "zh-Hant": [
        "理工大學附近最近的便利店在哪裏?",
        "從旺角到中環怎麼去?",
        "灣仔站下一班列車甚麼時候到?",
        "又一城附近有沒有公共廁所?",
    ],
    "zh-Hans": [
        "理工大学附近最近的便利店在哪里?",
        "从旺角到中环怎么走?",
        "湾仔站下一班列车什么时候到?",
        "又一城附近有没有公共厕所?",
    ],
}


def _system_prompt_for(lang: str) -> str:
    style = _LANG_STYLE.get(lang, _LANG_STYLE["en"])
    examples = "\n".join(f"  - {q}" for q in _FEW_SHOT.get(lang, _FEW_SHOT["en"]))
    return (
        "You are a question generator for a Hong Kong smart-city assistant. "
        "Generate DIVERSE, REALISTIC questions a Hong Kong resident or "
        "visitor might ask. The questions will be sent to an agent that has "
        "access to live HK government data + OpenStreetMap.\n\n"
        f"LANGUAGE: {lang}. {style}\n\n"
        "Style requirements:\n"
        "- Mix question forms.\n"
        "- Include SPECIFIC HK places: districts, MTR stations, malls, "
        "neighbourhoods, streets, landmarks, universities, parks.\n"
        "- Vary phrasing — no two questions should be near-rewordings.\n"
        "- Each question must be answerable using ONLY the dataset described.\n"
        "- Single sentence, ≤ 25 words.\n\n"
        f"Example questions in {lang}:\n{examples}\n\n"
        "Return ONLY a JSON array of question strings. No preamble, no "
        "commentary, no markdown code fence. Just the array."
    )


def _user_prompt(target: GenerationTarget, batch_size: int, lang: str) -> str:
    return (
        f"Dataset: {target.description_for_prompt}\n\n"
        f"Generate {batch_size} distinct questions in {lang!r} about THIS "
        "dataset, following the language and style rules above."
    )


async def _gemma_complete(
    client: httpx.AsyncClient,
    *,
    lm_base_url: str,
    model: str,
    target: GenerationTarget,
    batch_size: int,
    lang: str,
    temperature: float = 0.95,
) -> list[str]:
    """One Gemma call for one (target, lang) cell. Returns the parsed list
    of question strings, or an empty list on failure (logged)."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _system_prompt_for(lang)},
            {"role": "user", "content": _user_prompt(target, batch_size, lang)},
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


_DEFAULT_LANGS: tuple[str, ...] = ("en", "yue", "zh-Hant", "zh-Hans")


async def generate(
    *,
    count: int,
    gemma_model: str,
    lm_base_url: str,
    output_path: Path,
    batch_size: int = _DEFAULT_BATCH,
    concurrency: int = 2,
    languages: tuple[str, ...] = _DEFAULT_LANGS,
) -> int:
    """Generate `count` questions stratified across the catalog AND the
    requested languages, append-write each as a JSONL row to `output_path`.
    Returns the number of questions actually written (may be < `count` if
    Gemma failures or duplicate detection trim the pool).

    Each (target, language) pair gets roughly count / (N_targets *
    N_languages) questions, so the corpus has even per-language coverage
    even if a generator call now and then returns short.
    """
    catalog = _load_catalog()
    targets = _build_targets(catalog)
    n_cells = len(targets) * len(languages)
    per_cell = max(1, count // n_cells)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    seen: set[str] = set()
    written = 0
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        async def _gen_one_batch(
            target: GenerationTarget, n: int, lang: str
        ) -> list[str]:
            async with sem:
                return await _gemma_complete(
                    client,
                    lm_base_url=lm_base_url,
                    model=gemma_model,
                    target=target,
                    batch_size=n,
                    lang=lang,
                )

        tasks: list[tuple[GenerationTarget, str, asyncio.Future[list[str]]]] = []
        for target in targets:
            for lang in languages:
                n_batches = (per_cell + batch_size - 1) // batch_size
                for _ in range(n_batches):
                    tasks.append(
                        (target, lang, _gen_one_batch(target, batch_size, lang))
                    )

        with output_path.open("a", encoding="utf-8") as fh:
            for target, lang, coro in tasks:
                qs = await coro
                for q in qs:
                    key = (lang + "|" + q.strip()).casefold()
                    if key in seen:
                        continue
                    seen.add(key)
                    row = {
                        "id": str(uuid.uuid4()),
                        "expected_dataset_id": target.dataset_id,
                        "expected_dataset_title": target.title,
                        "expected_dataset_category": target.category,
                        "expected_tools": list(target.expected_tools),
                        "question_en": q,  # kept as field name for back-compat
                        "language": lang,
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
                    f"(seen={len(seen)}, last={target.dataset_id} {lang})      ",
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
    parser.add_argument(
        "--languages",
        default=",".join(_DEFAULT_LANGS),
        help="Comma-separated language codes (en, yue, zh-Hant, zh-Hans).",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    langs = tuple(s.strip() for s in args.languages.split(",") if s.strip())
    written = asyncio.run(
        generate(
            count=args.count,
            gemma_model=args.gemma_model,
            lm_base_url=args.lm_base_url,
            output_path=args.out,
            batch_size=args.batch_size,
            concurrency=args.concurrency,
            languages=langs,
        )
    )
    print(f"wrote {written} questions to {args.out} (langs={langs})", file=sys.stderr)
    return 0 if written > 0 else 1


__all__ = ["GenerationTarget", "generate", "main"]


if __name__ == "__main__":
    sys.exit(main())
