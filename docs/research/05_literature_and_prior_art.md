# Literature & Prior-Art Review — HK Smart City Agent for Social Robotics

**Project:** HK Smart City Integration — Lab of Social Robotics
**Compiled:** 2026-04-21
**Coverage window:** 2022–2026

> Primary search: arXiv, Google Scholar, ACL Anthology, GitHub, HK gov sources. Verify any URL before citing in a paper — some arXiv IDs are very recent.

---

## 1. Academic Papers

### 1.1 LLM agents for urban / smart-city query answering

**[P1] LLM Agents for Smart City Management — MDPI Smart Cities 2025**
Kalyuzhnaya et al. · https://www.mdpi.com/2624-6511/8/1/19
Multi-agent architecture combining LLMs with existing urban info systems; tested on St. Petersburg's Digital Urban Platform. 94–99% pipeline-selection accuracy; RAG delivers 17–55% accuracy gain (G-Eval 0.68–0.74 vs 0.30–0.38 baseline).
*Borrow:* routing-then-RAG two-stage pattern; G-Eval for open-ended answers. *Avoid:* bespoke schema — map to data.gov.hk instead.

**[P2] UrbanLLM — arXiv 2406.12360 (2024)**
Jiang et al. · https://arxiv.org/html/2406.12360v1
LLaMA-2-7B fine-tuned to decompose NL city queries into subtasks, delegate to specialist models, synthesize responses.
*Borrow:* decompose-then-delegate maps directly to our tool-call orchestration. *Avoid:* fine-tuning cost — prompt-based decomposition first.

**[P3] Urban Computing in the Era of LLMs — arXiv 2504.02009 (2025)**
https://arxiv.org/html/2504.02009v1
Survey across traffic / mobility / environment / land use / POI. Taxonomy of understanding, generation, decision-making, evaluation.
*Borrow:* taxonomy for tool-call types + gap list (real-time grounding, multilingual).

**[P4] LLM-Powered Intelligent Urban Agents — arXiv 2507.00914 (2025)**
USAIL-HKUST et al. · https://arxiv.org/html/2507.00914v1
Framing of "urban LLM agents" embedded in cyber-physical-social space.
*Borrow:* cyber/physical/social perception triad clarifies what a robot needs to sense.

**[P5] LLMs as Urban Residents — arXiv 2402.14744 (2024)**
https://arxiv.org/html/2402.14744v2
Agents modelling individual mobility preferences for simulation.
*Borrow:* preference-slot modelling informs session state (stored origin, mode preference, accessibility needs).

### 1.2 Multimodal / multilingual transport assistants

**[P6] Beyond Words: Evaluating LLMs in Transportation Planning — Taylor & Francis 2025**
https://www.tandfonline.com/doi/full/10.1080/10095020.2025.2493073
Larger models don't uniformly beat smaller ones on domain-specific transport reasoning — structured prompting matters more.
*Borrow:* rubric (factual accuracy + reasoning quality + hallucination rate) for our 20-question test suite.

**[P7] Integrating LLMs with ITS — arXiv 2501.04437 (2025)**
https://arxiv.org/html/2501.04437v1
Argues streaming/push-based RAG beats static vector stores for real-time transit data.
*Borrow:* justifies our real-time overlay strategy for MTR/bus alerts + HKO warnings.

**[P8] LLMs for Traffic & Transportation Research — arXiv 2503.21330 (2025)**
https://arxiv.org/pdf/2503.21330
Multilingual pipelines for linguistically diverse communities.
*Borrow:* maps to our Cantonese > Mandarin > English priority chain.

### 1.3 Social robot dialog grounded in live city data

**[P9] Humanoid Robot Social Interaction in Smart City — Frontiers 2022**
https://www.frontiersin.org/journals/sustainable-cities/articles/10.3389/frsc.2022.1076101/full
Confirms sub-second latency is critical for natural robot conversation; rule-based dialog breaks when city data changes.
*Borrow:* validates our 1.5 s budget; design for graceful degradation.

**[P10] Knowledge-Grounded Dialogue Flow Management for Social Robots — Grassi 2022**
https://pmc.ncbi.nlm.nih.gov/articles/PMC8932468/
Separate flow control (ontology / state machine) from surface generation (LLM).
*Borrow:* keep LLM as the "voice", keep a deterministic slot-filling state machine as the "brain" for transport queries.

**[P11] Building for Speech: Social Robots & Audio — Frontiers 2024**
https://www.frontiersin.org/journals/robotics-and-ai/articles/10.3389/frobt.2024.1356477/full
Latency + turn-taking + prosody are under-researched; Cantonese prosody (six tones, sandhi, sentence-final particles 囉/喎/㗎) needs explicit handling upstream.

**[P12] MTR "Tracy" Virtual Service Ambassador (2024) — industry**
MTR / Set Sail AI / Pantheon Lab · https://www.mtrlab.com.hk/en/news/663b3e072c4c8b31c11dd474/ · https://chatbot.com.hk/showcase-mtr-tracy-aikiosk.html
Physical AI kiosk at Kai Tak / Yau Ma Tei. Voice-controlled, trilingual (Cantonese/Mandarin/English). First AI virtual ambassador in HK transport.
*Takeaway:* Tracy is the closest existing peer. Cantonese-first is already validated with HK commuters; our bar is ≥ Tracy on wayfinding + facilities + local attractions.

### 1.4 Evaluation of agentic systems with vague intents

**[P13] Beyond Task Completion: Assessment Framework for Agentic AI — arXiv 2512.12791**
https://arxiv.org/abs/2512.12791
Four pillars: LLM quality, Memory, Tools, Environment. Binary task success insufficient.
*Borrow:* adopt the four-pillar decomposition for our eval harness.

**[P14] Evaluation & Benchmarking of LLM Agents — arXiv 2507.21504 (2025)**
https://arxiv.org/html/2507.21504v1
Meta-survey of 84 papers. Recommends five-axis balanced eval: capability/efficiency · robustness/adaptability · safety/ethics · human-centred · economic.
*Borrow:* robustness axis (graceful handling of ambiguous place names like "銅鑼灣" vs "Times Square") + safety axis (no hallucinated routes).

**[P15] USTBench — USAIL-HKUST arXiv 2505.17572 (2025)**
https://arxiv.org/abs/2505.17572 · https://github.com/usail-hkust/USTBench
62,466 QA pairs across five urban decision-making + four spatiotemporal prediction tasks. Reasoning-heavy models don't consistently beat simpler LLMs on urban tasks — domain specialisation wins.
*Takeaway:* validates choosing gpt-oss-120b + strong tool schemas over a bigger reasoning model.

### 1.5 HK-specific smart-city deployments

**[P16] Smart City Implementation Models in HK/Macao/Shenzhen — Emerald PAP 2024**
https://www.emerald.com/insight/content/doi/10.1108/pap-05-2024-0074/full/html
HK's model: strong gov coordination, place-specific adaptation, weaker community engagement than Shenzhen.
*Takeaway:* community-facing interfaces (like a robot) are a recognised gap — our build directly addresses it.

**[P17] HK Smart City Blueprint 2.0 — HKSAR Govt 2020**
https://www.smartcity.gov.hk/modules/custom/custom_global_js_css/assets/files/HKSmartCityBlueprint(ENG)v2.pdf
Six domains; commits to data.gov.hk + GTFS/API formats.
*Takeaway:* authoritative confirmation that transport + facilities + AQI + weather are open; housing data is partial.

**[P18] KPMG "Hong Kong's Connected Future" 2022**
https://assets.kpmg.com/content/dam/kpmg/cn/pdf/en/2022/01/hong-kong-s-connnected-future.pdf
Residents' top priorities: transport 68% · public safety 61% · healthcare 54%. Cantonese service flagged as trust driver.
*Takeaway:* validates our priority ordering.

---

## 2. Industry deployments & OSS projects

**[I1] MTR Mobile — https://www.mtr.com.hk/chatbot/**
Trip planning, ETAs, car occupancy, chatbot; trilingual.

**[I2] HKChat — https://hkchat.org/**
Community Cantonese-first assistant over HK gov data. Potential benchmark peer.

**[I3] Google Maps Grounding in Vertex AI (2024) — https://mapsplatform.google.com/resources/blog/grounding-with-google-maps-now-available-in-vertex-ai-power-your-ai-responses-with-google-maps-information/**
MCP-based grounding pattern. US-only; we build the HK analogue over data.gov.hk.

**[I4] Citymapper AI Personalisation — https://citymapper.com/**
Preference learning for routes. Maps to our session-memory slots.

**[I5] data.gov.hk — https://data.gov.hk/en/ · API: https://data.gov.hk/en/help/api-spec**
Primary data source. Undocumented rate limits → cache + back-off from day one.

**[I6] MTR AI Service Robots (2023–2024) — https://www.railway-technology.com/news/hong-kong-mtr-to-use-robots-to-enhance-customer-service/**
Closest existing deployment to our end-state. Validates Cantonese voice at station level.

**[I7] SoftBank Pepper / NAO — now under Maxvision after Aldebaran's 2025 receivership**
Lesson: don't hard-depend on any robot SDK; expose a hardware-agnostic agent interface.

**[I8] UBTECH Walker / Galbot (HK, 2023–2024)**
Revenue-service focus. Build for hardware abstraction.

### 2.3 HK university labs — local prior art (highest-leverage)

**[I9] USAIL-HKUST — Urban AI Systems & Intelligence Lab**
https://github.com/usail-hkust
- USTBench (NeurIPS 2025 workshop / arXiv 2505.17572) — see P15
- UrbanKGent (NeurIPS 2024) — LLM agent for urban KG construction, 20× cheaper than GPT-4 on KGC. https://arxiv.org/html/2402.06861v2 · https://github.com/usail-hkust/UrbanKGent
- UUKG — unified urban KG dataset with HK spatiotemporal data. https://github.com/usail-hkust/UUKG
- Awesome-Urban-LLM-Agents reading list. https://github.com/usail-hkust/Awesome-Urban-LLM-Agents
*Takeaway:* **same campus ecosystem, same problem space** — strong candidate for collaboration / KG reuse.

**[I10] HKUST HLTC — https://cse.hkust.edu.hk/~hltc/about.html**
Pascale Fung / Helen Meng — Cantonese ASR, robust telephone speech, cross-lingual dialog. Corpus + acoustic-model heritage.

**[I11] PolyU IMCL — https://www4.comp.polyu.edu.hk/~labimcl/nlp.html**
Jing Li et al. · Cantonese NLP in the transformer era. AACL 2022 talk: https://www4.comp.polyu.edu.hk/~jing1li/talks/aacl2022-can-pretrain/Slides.pdf
*Takeaway:* code-switching is the dominant HK input mode. Tokenisers + classifiers must handle Canto + English + Jyutping within the same message.

**[I12] Cantonese NLP Survey in the Transformers Era — Springer LRE 2024**
https://link.springer.com/article/10.1007/s10579-024-09744-w
First comprehensive Cantonese NLP survey. Documents spoken/written divergence, LIHKG/OpenRice as colloquial corpora. Cantonese remains low-resource despite 85M+ speakers.

**[I13] "How Well Do LLMs Handle Cantonese?" — arXiv 2408.16756, NAACL 2025 Findings**
Jiang et al., HKU · https://arxiv.org/abs/2408.16756 · https://i.cs.hku.hk/~cwu/papers/jyjiang-naacl25.pdf
Most current LLMs are significantly worse on Cantonese than on Mandarin or English; colloquial Cantonese is hardest.
*Takeaway:* **use this benchmark as a pre-deployment gate** for the backbone model.

**[I14] Large-Scale Cantonese Dataset for LLM Multi-Tasking — arXiv 2503.03702 (2025)**
https://arxiv.org/html/2503.03702v1
Multi-task Cantonese instruction dataset.
*Takeaway:* candidate for LoRA fine-tuning or eval supplementation, not primary training.

---

## 3. Evaluation & UX frameworks

**[E1] Multi3WOZ — MIT TACL 2023**
https://direct.mit.edu/tacl/article/doi/10.1162/tacl_a_00609/118298/
Multilingual multi-domain task-oriented dialog. 494k turns / 4 languages. JGA, Slot-Filling F1, Dialog Success Rate.
*Borrow:* JGA + Slot F1 as primary metrics. Extend with Cantonese via bottom-up dialog creation (not translation).

**[E2] MultiWOZ 2.4 — ACL SIGDIAL 2022**
https://aclanthology.org/2022.sigdial-1.34/
Corrected multi-domain dialogs. Taxi/train domains map to our transport queries.

**[E3] RAGAS — EACL 2024**
https://www.articsledge.com/post/retrieval-augmented-generation-assessment-system-ragas
Faithfulness, answer relevance, context precision/recall. Target end-to-end < 3 s for interactive use.
*Borrow:* RAGAS faithfulness as primary hallucination guard; add deterministic checks (e.g. bus-stop codes must appear in a verified list).

### Our eval metrics (targets)

| Metric | Definition | Target |
|---|---|---|
| Task Success Rate (TSR) | % turns where intent correctly fulfilled | ≥ 90% |
| Slot-Filling F1 | origin / dest / mode / date extraction | ≥ 0.85 |
| Disambiguation Recall | % ambiguous queries where clarification is asked | ≥ 95% |
| Tool Selection Accuracy | correct data.gov.hk endpoint called | ≥ 94% |
| RAGAS Faithfulness | answer ≠ contradict API response | ≥ 0.90 |
| Cantonese Detection | correct language tag | ≥ 95% |
| Median turn latency | p50 wall-clock | ≤ 1.5 s |
| p95 turn latency | p95 wall-clock | ≤ 3 s |
| Slot persistence | multi-turn slot carry-over | ≥ 99% |
| Graceful degradation | % API-fail turns with useful partial reply | ≥ 80% |

### Test-set sources

| Dataset | Coverage | Cantonese? | URL |
|---|---|---|---|
| USTBench | HK/urban spatiotemporal QA (62K) | No → adapt | https://github.com/usail-hkust/USTBench |
| HKU Cantonese Benchmark | Factual / logic / reasoning | Yes | https://arxiv.org/abs/2408.16756 |
| Multi3WOZ | Multi-domain task-oriented dialog (4 langs) | No → extend | https://direct.mit.edu/tacl/article/doi/10.1162/tacl_a_00609/118298/ |
| UUKG | HK urban KG | No | https://github.com/usail-hkust/UUKG |
| PolyU IMCL / LIHKG corpora | Colloquial Cantonese | Yes | via PolyU |

---

## 4. Top 10 takeaways that should shape our build

1. **Routing-then-RAG beats monolithic LLM** (P1). Domain router first, then tool-grounded answer.
2. **Cantonese is a first-class language** — not a Mandarin dialect. Tokeniser, embedder, intent classifier, TTS locale all Canto-aware (P11/P12/I13/I14).
3. **MTR Tracy is the live benchmark peer** (P12). We ship ≥ its scope in Cantonese first.
4. **Engage USAIL-HKUST** (I9). UUKG + UrbanKGent could accelerate grounding significantly.
5. **Latency 1.5 s / 3 s is empirically grounded** (P9). Stream tokens to TTS; cache hot intents (next-train).
6. **Separate flow control (state machine) from generation (LLM)** (P10). Deterministic slots for transport; LLM for surface form.
7. **Reasoning-heavy models don't always win on urban tasks** (P15). gpt-oss-120b + strong schemas is defensible.
8. **Instrument every eval axis from day one** (P13, E3). Track tool accuracy, slot persistence, faithfulness, latency — not just pass/fail.
9. **Graceful degradation is a first-class feature** (P9, P13). Typhoon / MTR closure / rate limit → degrade tree, never silent hallucination.
10. **Hardware-agnostic agent interface from day one** (I7, I8). Robot ecosystems churn — Aldebaran's 2025 receivership is the case in point.

---

## Executive summary (~350 words)

This review synthesises 18 academic papers, 14 industry/OSS projects, and 3 evaluation frameworks directly relevant to a Cantonese-first agentic city-query system for Hong Kong.

The strongest academic signal comes from **USAIL-HKUST's own lab** (USTBench, UrbanKGent, UUKG) — a peer group on the same campus ecosystem working on near-identical urban LLM agent problems with HK data. Engaging them for data sharing should be an early priority. The MDPI 2025 multi-agent city management paper validates a **routing-then-RAG** architecture at 94–99% routing accuracy and 17–55% RAG-driven accuracy gains — the correct orchestrator skeleton.

On Cantonese: three converging findings dominate: (1) current LLMs perform significantly worse on Cantonese than Mandarin or English (HKU benchmark, arXiv 2408.16756); (2) colloquial Cantonese and Canto-English code-switching are the hardest cases (PolyU IMCL, Springer LRE survey); (3) a new multi-task Cantonese dataset (arXiv 2503.03702) is available for instruction-tuning or evaluation. The backbone LLM must be validated against the HKU Cantonese benchmark before deployment, not after.

**MTR's Tracy (2024)** is the clearest industry benchmark — a trilingual Cantonese-first AI kiosk already live at Kai Tak and Yau Ma Tei stations, covering wayfinding, facilities, and local attractions. Its existence validates market readiness and raises the quality bar.

Evaluation frameworks converge on five non-negotiable metrics: Task Success Rate (≥ 90%), Slot-Filling F1 (≥ 0.85), Tool Selection Accuracy (≥ 94%), RAGAS Faithfulness (≥ 0.90), and latency (p50 ≤ 1.5 s, p95 ≤ 3 s). USTBench and the HKU Cantonese benchmark are usable as test sets today; Multi3WOZ provides the dialog-eval template to localise.

The ten takeaways reduce to three imperatives:
(a) **build routing-then-RAG**, not a monolith;
(b) **treat Cantonese as first-class** end-to-end (tokeniser → classifier → generation → TTS locale);
(c) **expose a hardware-agnostic agent interface** — robot hardware ecosystems are volatile, as Aldebaran's 2025 bankruptcy underscores.

Key citations: [MDPI Smart Cities 2025](https://www.mdpi.com/2624-6511/8/1/19), [USTBench arXiv 2505.17572](https://arxiv.org/abs/2505.17572), [HKU Cantonese benchmark arXiv 2408.16756](https://arxiv.org/abs/2408.16756), [Cantonese NLP Survey 2024](https://link.springer.com/article/10.1007/s10579-024-09744-w), [MTR Tracy](https://www.mtrlab.com.hk/en/news/663b3e072c4c8b31c11dd474/), [HK Smart City Blueprint 2.0](https://www.smartcity.gov.hk/modules/custom/custom_global_js_css/assets/files/HKSmartCityBlueprint(ENG)v2.pdf), [data.gov.hk API spec](https://data.gov.hk/en/help/api-spec).
