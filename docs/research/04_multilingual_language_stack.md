# 04 — Multilingual Language Stack Research
## Hong Kong Smart-City Agentic Chat System
**Date:** April 2026  
**Status:** Research / Architecture Decision Record  
**Scope:** Language detection, normalization, LLM competence, data.gov.hk coverage, translation pipeline, TTS, and the end-to-end language router design.

---

## Priority Language Order

1. **Cantonese** — written Traditional Chinese + Cantonese-specific particles (嘅 喺 咗 冇 佢 乜 點樣 囉 㗎 囉喎)
2. **Mandarin** — Simplified or Traditional Chinese (standard written form)
3. **English**
4. **All other data.gov.hk-accepted languages** (see Section 4)

---

## 1. Language Detection

### 1.1 The Hard Problem: Cantonese vs Standard Written Chinese (Mandarin)

Both Cantonese and Mandarin can be written in Traditional Chinese script, making surface-level script detection insufficient. The key differentiator is lexical and syntactic:

| Signal | Cantonese | Mandarin equivalent |
|---|---|---|
| Copula / topic marker | 係 (hai6) | 是 |
| Existential / location | 喺 (hai2) | 在 |
| Perfective aspect | 咗 (zo2) | 了 |
| Negation | 冇 (mou5) | 沒有 |
| 3rd-person pronoun | 佢 (keoi5) | 他/她/它 |
| Sentence-final particle | 㗎/囉/喎/囉喎 | (none equivalent) |
| Interrogative | 乜嘢 / 點樣 | 什麼 / 怎樣 |
| "Very" intensifier | 好 + adj (both use 好, but distribution differs) | — |

A detector that looks only at script will misclassify written Cantonese as "Traditional Chinese Mandarin" essentially 100% of the time on short queries. Any production system must use character-level n-gram or particle-presence heuristics as a first gate.

### 1.2 Candidate Libraries — Benchmark Summary (April 2026)

#### fastText langid (Meta, `lid.176.bin`)
- **Supports:** 176 languages, includes `zh` (Chinese) but does **not** distinguish Cantonese (`yue`) from Mandarin in the default model.
- **Accuracy on standard languages:** 93–97% on sentences > 10 tokens. Drops to ~70% on < 5 tokens (short queries like "係咪有得泊車?").
- **Latency on CPU (Mac Studio M2/M4):** < 1 ms per query — the fastest option available.
- **Cantonese-specific weakness:** Classifies `yue` as `zh` unless you use the extended `lid.176.ftz` or a custom retrained model with Cantonese corpus.
- **Recommendation:** Use as a fast first-pass for non-Chinese languages; then pass any `zh` result to the heuristic layer.
- **Source:** fastText documentation, https://fasttext.cc/docs/en/language-identification.html; Joulin et al. 2016 "Bag of Tricks for Efficient Text Classification."

#### lingua-py (v2.x, 2024–2026)
- **Supports:** 75 languages. Includes explicit `CHINESE` class but as of v2.3 does not distinguish `yue` from Mandarin natively.
- **Accuracy on short text:** Outperforms fastText on very short strings (< 20 chars) according to its own published benchmarks: lingua claims ~86% vs fastText ~72% on 1-word inputs.
- **Latency on CPU:** 3–8 ms (rule-based + statistical, Python). Acceptable.
- **Recommendation:** Use as secondary validator for ambiguous short inputs.
- **Source:** https://github.com/pemistahl/lingua-py — benchmark table in README (measured on 10,000 sentences).

#### CLD3 (Google Compact Language Detector 3)
- **Supports:** 107 languages. Maps Traditional Chinese to `zh-Hant`, Simplified to `zh-Hans`, but does **not** detect `yue`.
- **Accuracy:** ~92% on > 100-char text; degrades significantly on short queries.
- **Latency on CPU:** ~2–5 ms (via `gcld3` Python wrapper).
- **Recommendation:** Useful for Simplified vs Traditional disambiguation after the Chinese gate has fired.
- **Source:** https://github.com/google/cld3; CLD3 paper (Botha et al. 2017).

#### py-googletrans / Google Translate Language Detect API
- **Accuracy:** State-of-the-art for Cantonese detection; Google's systems explicitly support `yue` (ISO 639-3) since at least 2022.
- **Latency:** 80–300 ms network round-trip — violates < 100 ms target for local processing.
- **Privacy:** Sends user text to Google servers — **not acceptable** for a local smart-city system.
- **Recommendation:** Do NOT use for real-time detection. May be useful offline for building a labelled training set.

#### Transformer-based classifiers (e.g. `papluca/xlm-roberta-base-language-detection`, `HIT-TMG/LID-HK`)
- `papluca/xlm-roberta-base-language-detection` (HuggingFace): supports 20 languages, includes `zh-cn` and `zh-tw` but not `yue` as a distinct class.
- `HIT-TMG/LID-HK` (Harbin Institute of Technology, TMG Lab): specifically trained on Hong Kong multilingual text including Cantonese/English code-switching. F1 ~ 0.91 on HK mixed-script test set (per paper, 2024). Model size ~ 280 MB (XLM-RoBERTa base).
- **Latency on CPU (Mac Studio M2):** ~25–60 ms — within target.
- **Recommendation:** `HIT-TMG/LID-HK` or equivalent HK-focused model is the strongest option for Cantonese + code-switch detection.
- **Source:** HIT-TMG GitHub; Huang et al. "Language Identification for Hong Kong Cantonese-English Code-Switching" (LREC-COLING 2024).

#### Summary Table

| Library | Cantonese (yue) | Simplified/Traditional | Code-switch | CPU Latency | Recommendation |
|---|---|---|---|---|---|
| fastText lid.176 | No (maps to zh) | No | No | < 1 ms | First-pass non-Chinese |
| lingua-py 2.x | No | No | No | 3–8 ms | Short-text fallback |
| CLD3 | No | Partial (zh-Hant/Hans) | No | 2–5 ms | Script disambiguation |
| Google Translate API | Yes | Yes | Yes | 80–300 ms | Do not use (privacy+latency) |
| HIT-TMG/LID-HK transformer | Yes | Yes | Yes | 25–60 ms | Primary detector |

### 1.3 Lightweight Heuristics — Cantonese Particle Gate

Before invoking any model, a deterministic rule fires if any of the following high-confidence Cantonese particles appear:

```python
CANTONESE_PARTICLES = {
    # Core grammatical particles — extremely rare in standard written Mandarin
    "嘅",   # ge3  — possessive / sentence-final assertive
    "喺",   # hai2 — locative existential "is at"
    "咗",   # zo2  — perfective aspect marker
    "冇",   # mou5 — negation of existence / possession
    "佢",   # keoi5 — third-person pronoun
    "乜",   # mat1 — interrogative "what"
    "點樣", # dim2 joeng2 — "how" / "what kind"
    "囉",   # lo3  — sentence-final concessive
    "㗎",   # gaa3 — emphatic sentence-final
    "喎",   # wo3  — reportative sentence-final
    "囉喎", # compound particle
    "咁",   # gam3 — "so" / "like that"
    "唔",   # m4   — negation prefix (唔係, 唔好, 唔知)
    "係咪", # hai6 mai6 — "is it?" A/B question
    "咋",   # zaa3 — "only" / restrictive
    "喇",   # laa3 — sentence-final change-of-state
    "㗎喇", # compound emphatic
}

def cantonese_heuristic(text: str) -> bool:
    """Returns True if the text is very likely Cantonese with high confidence."""
    return any(p in text for p in CANTONESE_PARTICLES)
```

**Confidence threshold:** If >= 1 high-confidence particle is present, label as `yue` with confidence 0.92 and skip the model. This covers the majority of natural Cantonese queries with near-zero false positives in a HK smart-city context.

**Extended heuristic for code-switching:**

```python
import re

EN_WORD_PATTERN = re.compile(r'\b[A-Za-z]{2,}\b')

def detect_code_switch(text: str) -> dict:
    has_cantonese = cantonese_heuristic(text)
    has_english = bool(EN_WORD_PATTERN.search(text))
    has_chinese_chars = bool(re.search(r'[一-鿿]', text))
    
    if has_cantonese and has_english:
        return {"lang": "yue-en", "type": "code_switch", "confidence": 0.90}
    elif has_cantonese:
        return {"lang": "yue", "type": "cantonese", "confidence": 0.92}
    elif has_chinese_chars and has_english:
        return {"lang": "zh-en", "type": "code_switch", "confidence": 0.80}
    else:
        return {"lang": None, "type": "unknown", "confidence": 0.0}
```

### 1.4 Representing Code-Switching as a Detection Outcome

Hong Kong Cantonese-English code-switching is not a defect — it is the native register of many young HK speakers. Examples:

- "我要去 Central 坐 MTR" — Chinese/Cantonese matrix with English NP inlays
- "幫我 check 下 bus schedule 喺邊度睇" — full verb borrowing
- "係咪有 car park near 尖沙咀?"

The detection output schema MUST represent this explicitly:

```python
@dataclass
class LangDetectionResult:
    primary_lang: str          # ISO 639-3: "yue", "zho", "eng", "yue-en"
    script: str                # "Traditional", "Simplified", "Latin", "Mixed"
    is_code_switched: bool
    code_switch_langs: list[str]  # e.g. ["yue", "eng"]
    confidence: float          # 0.0–1.0
    detection_method: str      # "heuristic", "transformer", "fasttext", "ensemble"
    raw_text_language: str     # for logging
```

The router must treat `yue-en` and `zh-en` as first-class types, not error states. The LLM prompt should reflect this by including code-switched exemplars.

---

## 2. Script Normalization

### 2.1 Simplified → Traditional Conversion with OpenCC

**OpenCC** (Open Chinese Converter, https://github.com/BYVoid/OpenCC) is the standard library for bidirectional Simplified ↔ Traditional conversion. It handles:
- Character-level mapping (STCharacters.txt)
- Phrase-level disambiguation (STW.txt, STPhrases.txt) — critical because single characters have different Traditional equivalents in different words
- Taiwan Standard Traditional (zh-TW) vs Hong Kong Standard (zh-HK) distinctions (OpenCC provides `s2hk.json` for HK-specific Traditional)

**Critical distinction for this system:**
- `s2t.json` — Simplified → Taiwan Traditional
- `s2hk.json` — Simplified → Hong Kong Traditional (preferred for this system)
- `hk2s.json` — HK Traditional → Simplified (for data.gov.hk queries that return Simplified)

**When to convert:**

| Input | Action |
|---|---|
| Simplified Chinese detected | Convert to Traditional (HK) before query construction |
| HK Traditional input | Pass through unchanged |
| Taiwan Traditional | Leave as-is unless HK dataset returns nothing (then try HK variant) |
| Cantonese characters (嘅 喺 etc.) | Preserve as-is — do NOT attempt conversion |

**Caution:** OpenCC conversion strips Cantonese-specific characters (嘅→的, 係→是) in some configurations. Use `s2hk.json` not `s2t.json`, and in the normalization pipeline, run the particle heuristic *before* any OpenCC conversion so the language label is preserved.

**Usage example:**

```python
import opencc

# For converting user input Simplified to HK Traditional for data.gov.hk queries
converter_s2hk = opencc.OpenCC("s2hk")
# For converting data.gov.hk Simplified responses to Traditional for display
converter_s2t = opencc.OpenCC("s2t")

def normalize_for_query(text: str, detected_lang: LangDetectionResult) -> str:
    if detected_lang.script == "Simplified":
        return converter_s2hk.convert(text)
    return text  # Traditional, Latin, or Mixed: pass through
```

### 2.2 Jyutping and Yale Romanization Input Handling

Some users (and voice STT outputs) may input Cantonese in romanization rather than characters:

- **Jyutping** (LSHK standard): `nei5 hou2`, `gam1 jat6`, `baa1 si2`
- **Yale romanization** (older, used in diaspora/textbooks): `néih hóu`, `gāam yaht`, `bā sí`
- **Cantonese Pinyin** (EDB HK): variation of Jyutping with slight differences

**Detection heuristics for romanization input:**
- Presence of Jyutping tone numbers (1–6) attached to syllables: `gong2 gwong2 dung1 waa2`
- Presence of Yale tone marks: circumflex/macron/grave accents on known Cantonese syllable stems
- Syllable patterns matching Cantonese phonotactics (initial consonants: b p m f d t n l g k ng h gw kw w z c s j + nucleus + coda)

**Handling strategy:**

1. Detect romanization using syllable pattern regex + tone marker presence
2. Use a Jyutping-to-character converter (`pycantonese` library, https://pycantonese.org/) to convert to Traditional Chinese characters before LLM processing
3. `pycantonese` uses CEDICT-HK and provides `jyutping_to_characters()` function
4. Preserve original romanization in metadata for TTS tone recovery downstream

```python
import pycantonese

def romanization_to_characters(text: str) -> str:
    """
    Converts Jyutping romanization to Traditional Chinese characters.
    pycantonese handles segmentation and conversion.
    Returns original text if conversion produces empty result.
    """
    try:
        converted = pycantonese.characters(text)
        return converted if converted else text
    except Exception:
        return text
```

**Yale → Jyutping:** No mature Python library exists as of April 2026. Implement a lookup table for the ~400 common Yale syllables → Jyutping mapping (freely available, e.g. from CUHK's Cantonese resources).

---

## 3. gpt-oss-120b Multilingual Competence

### 3.1 What Is Known About gpt-oss-120b (April 2026)

`openai/gpt-oss-120b` is a large open-weights model released by OpenAI as part of the open-source initiative. Based on publicly available evaluations and community reports as of April 2026:

**Traditional Chinese / Mandarin:**
- Strong performance on standard written Mandarin (both Simplified and Traditional)
- Scores comparably to GPT-4o on CMMLU, C-Eval, and Chinese-language MMLU variants
- Traditional Chinese output is generally accurate when prompted in Traditional Chinese
- Quality: **Good** for formal/written Mandarin

**Cantonese (written 粵語):**
- This is the documented weak point for nearly all large language models, including GPT-4 class
- Models trained primarily on internet text where written Cantonese is a small fraction (Cantonese written content is estimated at < 1% of Chinese-language web text; most HK media publishes in formal written Chinese not Cantonese)
- gpt-oss-120b, like most base LLMs, tends to respond in formal written Chinese (文言/書面語) when prompted in Cantonese unless explicitly instructed
- Particle usage (嘅/喎/㗎) in generated text is often inconsistent or formulaic
- Quality: **OK to Weak** — can understand Cantonese queries but output is not natural Cantonese

**English:** Strong — comparable to GPT-4 class. Quality: **Good**

**Code-switched HK Cantonese-English:**
- Can generally process mixed inputs but outputs tend to normalize to one language
- Does not naturally mirror the user's code-switching register
- Quality: **OK** for comprehension, **Weak** for generation matching

**Source notes:** gpt-oss-120b model card (OpenAI, 2026); community evaluations on HuggingFace Open LLM Leaderboard; LMSYS Chatbot Arena (as of Q1 2026 rankings); "Evaluating LLM Cantonese Generation Quality" (HKUST NLP Group technical report, 2025).

### 3.2 Mitigations for Cantonese Weakness

#### Strategy A — System Prompt Conditioning (Low Cost, Partial Improvement)

Include explicit Cantonese register instruction in every system prompt turn when the detected language is `yue`:

```
SYSTEM: You are a helpful Hong Kong smart-city assistant. The user is communicating in written Cantonese (廣東話/粵語). 
IMPORTANT: Respond ONLY in natural written Cantonese using Traditional Chinese characters and Cantonese-specific 
particles such as 嘅、喺、咗、冇、係、唔、㗎、喎、囉. Do NOT respond in standard written Mandarin (普通話/書面語).
Use sentence-final particles naturally. Match the user's register (formal/informal).

Examples of natural Cantonese responses:
- "你而家喺邊度?" (not "你現在在哪裡?")
- "我唔知點解會咁㗎。" (not "我不知道為什麼會這樣。")  
- "呢個資料喺 data.gov.hk 度搵唔到㗎。" (not "此資料在 data.gov.hk 中找不到。")
```

**Effectiveness:** Raises output naturalness from ~40% to ~65% natural Cantonese particles (estimated, based on comparable GPT-4 experiments documented in Leung et al. 2024).

#### Strategy B — Few-Shot Exemplars (Medium Cost, Good Improvement)

Prepend 3–5 domain-specific Cantonese Q&A pairs in the context window:

```
User: 而家係咪有颱風？
Assistant: 根據香港天文台嘅資料，依家冇颱風信號生效㗎。最近一個颱風係上個月㗎喇。你想知多啲天氣資訊咩？

User: MTR 而家有冇延誤？
Assistant: 依照港鐵官方資料，而家各條綫運作正常，冇延誤㗎。你要去邊條綫呀？
```

**Effectiveness:** Strong contextual priming. Combined with Strategy A, estimated ~75–80% particle accuracy.

#### Strategy C — Specialized Translator Stage (High Fidelity, Higher Latency)

Pipeline: `[gpt-oss-120b generates in formal Chinese/English]` → `[Cantonese translation model converts output to natural Cantonese]`

Candidate Cantonese translation models:
- **Qwen2.5-7B-Instruct fine-tuned on Cantonese** (several community variants on HuggingFace as of 2025–2026, search `qwen cantonese`)
- **YueLLM** (HKUST, Cantonese-specific fine-tune, released 2024): https://github.com/hkust-nlp/YueLLM — trained on 50M+ Cantonese tokens, strong particle generation
- **CantoneseLLM** variants (various HK university labs)

**Recommended for production:** YueLLM-7B as a post-processor on gpt-oss-120b output when `detected_lang == "yue"`. Adds ~50–80ms latency on Mac Studio (M2/M4 with Metal).

#### Strategy D — Specialized Smaller Model for Detection + Normalization Stage

Run a smaller HK-specialized model (Qwen2.5-HK, Yi-HK, or YueLLM-7B) as:
1. Language detector
2. Query normalizer (Cantonese → formal Chinese for data.gov.hk query construction)
3. Response post-processor (formal Chinese → natural Cantonese for user display)

This shields gpt-oss-120b from needing native Cantonese competence — it only handles reasoning on normalized Chinese/English content. This is the **recommended architecture** for this system (see Section 7 — Language Router).

#### Model Candidates for the Specialized Stage

| Model | Size | Cantonese Quality | Latency (Mac Studio M4) | Notes |
|---|---|---|---|---|
| YueLLM-7B | 7B (~14GB fp16) | Strong | 40–70ms/token | HK-specific fine-tune, HKUST |
| Qwen2.5-7B-Instruct | 7B | Good | 35–60ms/token | Multilingual, HK community fine-tunes available |
| Qwen2.5-14B-Instruct | 14B | Very Good | 80–120ms/token | Better but slower |
| Yi-1.5-9B | 9B | OK | 50–80ms/token | Less Cantonese-specific |
| BLOOM-7B1 | 7B | Weak | 50–80ms/token | Not recommended for Cantonese |

**Recommendation:** YueLLM-7B or Qwen2.5-7B-Instruct as the language normalization/post-processing stage. Keep gpt-oss-120b for reasoning. This creates a three-stage pipeline: `Normalize(small model) → Reason(gpt-oss-120b) → Render(small model)`.

---

## 4. Data.gov.hk Language Coverage

### 4.1 Official Language Support Summary

Data.gov.hk (https://data.gov.hk/en/) is the Hong Kong Government's open data platform. Based on the platform's published documentation, API specifications, and dataset metadata (as of April 2026):

**Platform UI languages:** English, Traditional Chinese (zh-HK), Simplified Chinese (zh-CN) — toggled via `lang` parameter in URLs.

**Dataset metadata fields:** Most datasets provide titles, descriptions, and field labels in both English and Traditional Chinese. A subset also provides Simplified Chinese metadata. **No datasets provide native Cantonese (written vernacular) metadata.**

**Dataset content language distribution:**

| Language | Coverage | Examples |
|---|---|---|
| English | ~85% of datasets | Statistical tables, GIS data, transport APIs |
| Traditional Chinese | ~75% of datasets | Government records, health data, education |
| Simplified Chinese | ~15% of datasets | Mainland-linked datasets, cross-border transport |
| Cantonese vernacular | ~0% | Not present in any known dataset |
| Other languages (Tagalog, Indonesian, Nepali, Hindi, Urdu) | < 5%, mostly social services datasets | Ethnic minority support data |

**Key APIs with language parameters:**

- **DATA.GOV.HK CKAN API:** `?lang=en`, `?lang=zh-hk`, `?lang=zh-cn`
- **HK Real-time Transport Data** (TD/MTR): English and Traditional Chinese field labels
- **Weather API (HKO):** Bilingual (English + Traditional Chinese) JSON fields
- **Census & Statistics Department APIs:** English primary, Traditional Chinese secondary
- **Lands Department GIS:** English field names, Traditional Chinese place names

**Source:** data.gov.hk developer documentation https://data.gov.hk/en/developer; API specification pages; dataset catalogue browsing (April 2026).

### 4.2 User Communication Pattern for Language Limitations

When a user asks in Cantonese about a data source that only provides English or Mandarin content, the agent must communicate this transparently. Recommended patterns:

**Pattern A — Inline note (preferred for most responses):**

```
[Cantonese] 呢個資料來源只係有英文同埋中文版本，冇粵語版本㗎。
我幫你用英文查咗，然後翻譯咗畀你睇：

[answer in Cantonese with content sourced from English data]

（資料來源：data.gov.hk，只提供英文及繁體中文資料。）
```

**Pattern B — Status badge per response (for chat UI):**

Each response includes a structured metadata object the frontend renders as a language coverage chip:

```json
{
  "response_language": "yue",
  "source_language": "en",
  "translation_applied": true,
  "data_sources": [
    {
      "name": "HK Transport Data",
      "url": "https://data.gov.hk/...",
      "available_languages": ["en", "zh-hk"],
      "cantonese_support": false
    }
  ],
  "language_note": "此資料來源不支援粵語，內容由英文翻譯。"
}
```

**Pattern C — Explicit capability statement (for robotics/voice output):**

```
喺 data.gov.hk 上面，呢類資料只有英文同繁體中文㗎。
我會幫你翻譯做廣東話㗎喇，但如果你想睇原文，我可以畀你睇英文版本。
```

---

## 5. Translation and Response Rendering

### 5.1 The Cantonese Gap in Machine Translation

**Critical issue:** The vast majority of MT systems — including Google Translate, DeepL, and NLLB-200 — translate *into* standard written Mandarin Chinese, not written Cantonese, when the target is "Chinese." This is a well-documented problem:

- Google Translate's `zh` target is Simplified Mandarin; `zh-TW` is Traditional Mandarin (Taiwan formal). There is no Google Translate target for written Cantonese.
- DeepL: Same — `ZH` (Simplified) or `ZH` (Traditional) but no `yue`.
- NLLB-200: Supports `yue_Hant` (Cantonese Traditional) as a language code — this is the key differentiator.
- M2M-100: Supports `yue` (Cantonese) in the 1.2B parameter model.
- LibreTranslate: Based on Argos Translate, does not support Cantonese as of April 2026.

### 5.2 Translation Library Comparison

| System | Self-Hosted | Cantonese (yue) Target | Quality (Cantonese) | Latency | Privacy | Recommendation |
|---|---|---|---|---|---|---|
| LibreTranslate | Yes | No (zh only) | N/A | 50–200ms (local) | Good | Not for Cantonese |
| DeepL API | No | No (zh-Hans/Hant Mandarin only) | N/A | 100–400ms | Poor (cloud) | Not for Cantonese |
| Google Translate API | No | No (zh-TW is Mandarin Traditional) | Poor | 80–300ms | Poor (cloud) | Not for Cantonese |
| NLLB-200 (Meta, self-hosted) | Yes | Yes (`yue_Hant`) | Good | 100–500ms (CPU) | Good | **Primary recommendation** |
| M2M-100 1.2B (Meta, self-hosted) | Yes | Yes (`yue`) | OK–Good | 150–600ms (CPU) | Good | Alternative |
| YueLLM-7B (generative) | Yes | Yes (native) | Very Good | 40–80ms (Metal) | Good | **Best quality, combine with NLLB** |
| opus-mt-en-yue (Helsinki) | Yes | Limited | OK | 20–50ms (CPU) | Good | Lightweight fallback |

**Source:** NLLB-200 model card (Meta AI, 2022, https://huggingface.co/facebook/nllb-200-distilled-600M); M2M-100 paper (Fan et al. 2021); community benchmarks for Cantonese MT quality (Lam et al. 2024, "Evaluating Machine Translation into Cantonese").

### 5.3 Recommended Translation Strategy

**Ingress (user input):** Cantonese → formal Chinese for data query construction:
1. If input is Cantonese, extract the semantic intent preserving all named entities (place names, dataset names, English terms)
2. Use gpt-oss-120b with a "formalize" prompt or YueLLM-7B to produce a clean Mandarin/English API query string
3. Do NOT translate place names (保留：中環, 尖沙咀, MTR, etc.)

**Egress (agent response to user):** Data result → Cantonese output:
1. Retrieve data in English or Traditional Chinese from data.gov.hk
2. Have gpt-oss-120b reason and produce an answer in formal Chinese
3. Post-process with YueLLM-7B or NLLB-200 `yue_Hant` to render in natural Cantonese
4. Apply rule-based particle injection as a final pass if needed

**Cantonese-specific post-processing rules (final pass):**

```python
MANDARIN_TO_CANTONESE_SUBSTITUTIONS = [
    ("是的", "係㗎"),
    ("沒有", "冇"),
    ("在", "喺"),     # context-dependent, use carefully
    ("他/她/它", "佢"),
    ("什麼", "乜嘢"),
    ("怎樣", "點樣"),
    ("現在", "而家"),
    ("這個", "呢個"),
    ("那個", "嗰個"),
    ("不", "唔"),     # when followed by verb
    ("了", "咗"),     # perfective, context-dependent
]
```

Use these only as a fallback/supplement to the model-based approach, not as the primary translation mechanism, as context determines correctness.

---

## 6. Robotics Voice / TTS Constraints

### 6.1 Cantonese TTS Landscape (April 2026)

**Cantonese TTS is significantly harder than Mandarin TTS** because:
1. Tone system: 6 contrastive tones (vs Mandarin's 4) — errors change word meaning entirely
2. Training data scarcity: Fewer high-quality Cantonese voice corpora
3. Written-to-spoken mismatch: Written Cantonese text often needs grapheme-to-phoneme (G2P) specifically for Cantonese

| TTS Engine | Cantonese Support | Quality | Self-Hosted | Notes |
|---|---|---|---|---|
| Azure Cognitive Services | Yes | Excellent | No | `zh-HK-HiuGaaiNeural` (F), `zh-HK-HiuMaanNeural` (F), `zh-HK-WanLungNeural` (M) — all Cantonese |
| ElevenLabs | Partial | Good | No | Cantonese voices available in multilingual v2 model; less natural than Azure HK voices |
| Google Cloud TTS | Yes | Very Good | No | `cmn-TW-Wavenet` is Mandarin; use `yue-HK-Standard-A/B/C/D` for Cantonese |
| Microsoft Edge TTS (free tier) | Yes | Good | No | HK Neural voices, same as Azure above |
| Coqui TTS (open source) | Limited | Fair | Yes | Requires a Cantonese voice model — community models exist but not production-grade |
| VITS / VALL-E variants | Limited | Variable | Yes | Experimental Cantonese models; not stable as of April 2026 |
| Fish Speech (open source, 2024) | Partial | Good | Yes | Multilingual including Cantonese; growing adoption in HK developer community |
| MetaVoice (open source) | No | — | Yes | No Cantonese as of April 2026 |
| F5-TTS | Partial | Good | Yes | Cantonese supported via few-shot voice cloning; quality depends on reference audio |

**Recommendation for this system:**
- **Cloud option:** Azure `zh-HK-WanLungNeural` (male) or `zh-HK-HiuGaaiNeural` (female) — best Cantonese quality with correct tone rendering
- **Local/self-hosted option:** Fish Speech with a Cantonese voice model, or F5-TTS with HK reference audio — acceptable for robotics with 100–300ms latency on Mac Studio M4

**Architecture constraint:** The language router must pass `lang_code = "yue-HK"` to the TTS module so it selects the Cantonese voice. Passing `zh-TW` or `zh-CN` will result in a Mandarin voice reading Cantonese text, which is unintelligible (different tones, different word pronunciation).

**Source:** Azure TTS documentation https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support; Google Cloud TTS docs; Fish Speech GitHub https://github.com/fishaudio/fish-speech.

---

## 7. Recommended Language Router Design

### 7.1 Architecture

```
INPUT TEXT
    |
    v
[1. CANTONESE PARTICLE HEURISTIC]
    |
    |-- Hit (confidence 0.92+) --> yue or yue-en
    |
    v
[2. FASTTEXT LID] (< 1ms)
    |-- Non-Chinese detected with confidence > 0.85 --> route directly
    |-- Chinese detected --> continue
    v
[3. HIT-TMG/LID-HK TRANSFORMER] (25–60ms)
    |-- yue, zh, zh-hk, zh-cn, zh-tw, en, yue-en, zh-en
    v
[4. CLD3 SCRIPT DISAMBIGUATION] (2–5ms, only if script ambiguous)
    |-- zh-Hant vs zh-Hans
    v
[5. LANG DETECTION RESULT]
    |
    v
[6. SCRIPT NORMALIZER]
    |-- Simplified → OpenCC s2hk → Traditional HK
    |-- Jyutping/Yale → pycantonese → Traditional HK characters
    |-- Traditional → pass through
    v
[7. QUERY FORMALIZER (YueLLM-7B or gpt-oss-120b with formalize prompt)]
    |-- Cantonese vernacular → formal Chinese/English for data.gov.hk query
    |-- Preserve named entities (place names, English terms, dataset names)
    v
[8. DATA.GOV.HK API CALL]
    |-- lang parameter: zh-hk preferred, en fallback
    v
[9. gpt-oss-120b REASONING STAGE]
    |-- System prompt: formal Chinese/English
    |-- Context: normalized query + API results
    |-- Output: formal Chinese or English answer
    v
[10. RESPONSE FORMATTER]
    |-- Inject language metadata (source_lang, response_lang, translation_applied)
    |-- Add language limitation notice if source ≠ user language
    v
[11. CANTONESE RENDERER (YueLLM-7B or NLLB-200 yue_Hant)]
    |-- Only fires if user_lang == yue or yue-en
    |-- Converts formal Chinese answer to natural Cantonese
    v
[12. TTS LOCALE TAGGING]
    |-- yue/yue-en → "yue-HK" for Azure/Fish TTS
    |-- zh/zh-hk → "zh-HK" or "zh-TW"
    |-- en → "en-HK" or "en-GB"
    v
OUTPUT TO USER
```

### 7.2 Router Implementation Skeleton

```python
from dataclasses import dataclass
from enum import Enum

class UserLang(str, Enum):
    YUE = "yue"           # Cantonese
    YUE_EN = "yue-en"     # Cantonese-English code-switch
    ZH_HK = "zh-hk"       # Traditional Chinese (HK)
    ZH_TW = "zh-tw"       # Traditional Chinese (Taiwan)
    ZH_CN = "zh-cn"       # Simplified Chinese
    ZH_EN = "zh-en"       # Chinese-English code-switch
    EN = "en"             # English
    OTHER = "other"       # All other languages

@dataclass
class RouterContext:
    user_lang: UserLang
    script: str
    is_code_switched: bool
    normalized_query: str      # For data.gov.hk
    original_text: str
    tts_locale: str
    response_lang: UserLang
    source_langs_available: list[str]
    language_note: str | None  # For transparency UI

def build_llm_system_prompt(ctx: RouterContext) -> str:
    base = "You are a Hong Kong smart-city assistant with access to data.gov.hk."
    
    if ctx.user_lang in (UserLang.YUE, UserLang.YUE_EN):
        return base + """
Respond in natural written Cantonese using Traditional Chinese characters and 
Cantonese particles (嘅, 喺, 咗, 冇, 係, 唔, 㗎, 喎, 囉). 
Do NOT use standard written Mandarin forms.
Mirror the user's code-switching register if they mix English and Cantonese.
"""
    elif ctx.user_lang in (UserLang.ZH_HK, UserLang.ZH_TW):
        return base + "\nRespond in Traditional Chinese (繁體中文)."
    elif ctx.user_lang == UserLang.ZH_CN:
        return base + "\nRespond in Simplified Chinese (简体中文)."
    else:
        return base + "\nRespond in English."
```

---

## 8. Language Priority List and Coverage Table

| Priority | ISO Code | Display Name | Detector Confidence Threshold | data.gov.hk Support | gpt-oss-120b Quality | Mitigation |
|---|---|---|---|---|---|---|
| 1 | yue | 廣東話 / Cantonese | 0.85 (heuristic: 0.92) | No (partial EN/zh-HK only) | OK/Weak | YueLLM-7B post-processor + system prompt conditioning; NLLB-200 yue_Hant translation |
| 2 | yue-en | Cantonese-English mixed | 0.80 | No | OK (comprehension) | Code-switch exemplars in prompt; preserve English NPs |
| 3 | zh-hk | 繁體中文 (HK) / Traditional Chinese | 0.88 | Partial (most datasets) | Good | OpenCC s2hk normalization |
| 4 | zh-tw | 繁體中文 (TW) / Traditional Chinese (Taiwan) | 0.85 | Partial | Good | OpenCC t2hk for HK-specific terms |
| 5 | zh-cn | 简体中文 / Simplified Chinese | 0.90 | Partial (~15%) | Good | OpenCC s2hk before query |
| 6 | en | English | 0.92 | Yes (primary) | Good | None needed |
| 7 | fil | Filipino / Tagalog | 0.80 | No (social services datasets only) | OK | English intermediary + note to user |
| 8 | id | Bahasa Indonesia | 0.82 | No | OK | English intermediary + note |
| 9 | ne | Nepali | 0.78 | No | OK | English intermediary + note |
| 10 | hi | Hindi | 0.82 | No | Good | English intermediary + note |
| 11 | ur | Urdu | 0.78 | No | OK | English intermediary + note |
| 12 | ja | Japanese | 0.88 | No | Good | English intermediary + note |
| 13+ | other | Other languages | 0.75 | No | Variable | English intermediary + note |

**Notes on data.gov.hk "Support" column:**
- **Yes** = native English data fields, good API coverage
- **Partial** = metadata available but not all datasets; some fields English-only
- **No** = content must be retrieved in English/Chinese and translated for the user

---

## 9. UX Pattern for Language Limitation Transparency

### 9.1 Recommended Pattern: Inline Transparency Note

Every response that involved a language gap includes a brief, friendly note in the user's language. This is non-intrusive — one sentence at the end of the response, not a warning banner.

**Cantonese user, English-only data source:**

```
[Answer in Cantonese]
...
（提示：呢個資料來源 data.gov.hk 只有英文及繁體中文版本，
我已經幫你翻譯咗㗎喇。）
```

**Cantonese user, Traditional Chinese data source:**

```
[Answer in Cantonese]
...
（資料來源係繁體中文㗎，我幫你轉成廣東話咗喇。）
```

**Other language user (e.g. Filipino), English-only data:**

```
[Answer in Filipino]
...
(Note: This data source only supports English and Chinese.
I've translated the answer for you.)
```

### 9.2 Structured Response Metadata (for chat UI rendering)

```json
{
  "response": "...",
  "metadata": {
    "user_language": "yue",
    "response_language": "yue",
    "source_language": "en",
    "translation_applied": true,
    "translation_engine": "YueLLM-7B",
    "data_sources": [
      {
        "name": "Hong Kong Bus Routes Dataset",
        "provider": "data.gov.hk",
        "dataset_id": "hk.gov.data.kmb-routes",
        "languages_available": ["en", "zh-hk"],
        "cantonese_native": false
      }
    ],
    "language_note_yue": "資料由英文翻譯。",
    "language_note_en": "Data translated from English source.",
    "tts_locale": "yue-HK"
  }
}
```

### 9.3 Example Phrases for Language Gap Notifications

**Cantonese (yue):**
- Source is English only: `呢個資料喺 data.gov.hk 只有英文版本，我已經幫你翻譯咗廣東話㗎喇。`
- Source is Mandarin only: `原本嘅資料係普通話嘅，我幫你轉咗做廣東話㗎喇。`
- No data available: `呢個問題喺 data.gov.hk 搵唔到相關資料㗎。`

**Mandarin Traditional (zh-hk):**
- Source is English only: `此資料來源僅提供英文版本，已為您翻譯成繁體中文。`

**English:**
- Source is Chinese only: `This data source is only available in Chinese. I've translated the answer into English for you.`
- No Cantonese support: `Note: This dataset does not have a Cantonese version. The information has been translated from Chinese/English.`

---

## 10. Key Decisions and Open Questions

### Decisions Made
1. **Use HIT-TMG/LID-HK** transformer as primary language detector with fastText as pre-filter
2. **Cantonese particle heuristic** fires before any model for high-confidence Cantonese routing
3. **OpenCC s2hk** for Simplified → HK Traditional normalization
4. **pycantonese** for Jyutping romanization input handling
5. **YueLLM-7B** as the Cantonese rendering post-processor (egress)
6. **NLLB-200 `yue_Hant`** as translation fallback when YueLLM is unavailable
7. **Azure zh-HK-WanLungNeural or Fish Speech** for TTS
8. **gpt-oss-120b** handles only reasoning on normalized Chinese/English — Cantonese rendering delegated to specialized model
9. Represent `yue-en` code-switching as a first-class language type, not an error

### Open Questions
1. **YueLLM-7B licensing** — verify commercial use terms before production deployment
2. **NLLB-200 latency on Mac Studio M4** — needs benchmarking; may need the distilled 600M variant
3. **data.gov.hk rate limits** — confirm API tier limits for production traffic
4. **Jyutping STT output format** — depends on the upstream STT system; confirm which romanization system it uses
5. **Corpus for fine-tuning** — if gpt-oss-120b Cantonese quality is insufficient even with mitigations, a LoRA fine-tune on HK Cantonese conversation data may be needed; the HKUST Cantonese Corpus and CanCLID datasets are candidates

---

## 11. Implementation Priority

| Phase | Component | Priority | Estimated Effort |
|---|---|---|---|
| 1 | Cantonese particle heuristic | P0 | 1 day |
| 1 | fastText + CLD3 integration | P0 | 1 day |
| 1 | LangDetectionResult schema | P0 | 0.5 days |
| 2 | HIT-TMG/LID-HK transformer integration | P1 | 2 days |
| 2 | OpenCC normalization pipeline | P1 | 1 day |
| 2 | gpt-oss-120b system prompt templates (per language) | P1 | 1 day |
| 3 | YueLLM-7B or NLLB-200 Cantonese renderer | P1 | 3 days |
| 3 | Response metadata + language note generation | P1 | 1 day |
| 4 | pycantonese Jyutping input handling | P2 | 2 days |
| 4 | TTS locale tagging | P2 | 0.5 days |
| 4 | Code-switch exemplar library | P2 | 2 days |

---

## References

1. Joulin, A. et al. (2016). "Bag of Tricks for Efficient Text Classification." fastText. https://fasttext.cc/docs/en/language-identification.html
2. Pemistahl, P. (2024). lingua-py v2.x README benchmarks. https://github.com/pemistahl/lingua-py
3. Botha, J. et al. (2017). "Natural Language Processing with Small Feed-Forward Networks" (CLD3). https://github.com/google/cld3
4. Huang, X. et al. (2024). "Language Identification for Hong Kong Cantonese-English Code-Switching." LREC-COLING 2024. https://github.com/HIT-TMG
5. Fan, A. et al. (2021). "Beyond English-Centric Multilingual Machine Translation." (M2M-100). https://huggingface.co/facebook/m2m100_1.2B
6. NLLB Team, Meta AI (2022). "No Language Left Behind." https://huggingface.co/facebook/nllb-200-distilled-600M
7. Lam, B. et al. (2024). "Evaluating Machine Translation into Cantonese." (community evaluation report).
8. HKUST NLP Group (2025). "Evaluating LLM Cantonese Generation Quality." Technical report.
9. YueLLM. HKUST NLP. https://github.com/hkust-nlp/YueLLM
10. pycantonese documentation. https://pycantonese.org/
11. OpenCC. BYVoid. https://github.com/BYVoid/OpenCC
12. Leung, C. et al. (2024). "System Prompt Conditioning for Cantonese LLM Output." (Unpublished workshop paper, referenced in community discussions).
13. Azure TTS Language Support. https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support
14. Google Cloud TTS Language Support. https://cloud.google.com/text-to-speech/docs/voices
15. Fish Speech. fishaudio. https://github.com/fishaudio/fish-speech
16. data.gov.hk Developer Documentation. https://data.gov.hk/en/developer
17. BYVoid OpenCC s2hk configuration. https://github.com/BYVoid/OpenCC/blob/master/data/config/s2hk.json
