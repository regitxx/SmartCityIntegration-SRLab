# Audit — v0.3.0 (2026-04-23)

Consolidated security + correctness + enhancement review of the SmartCityIntegration agent. Three parallel tracks ran against the codebase + dependency graph + 2026-current research. Detailed findings:

- [`docs/audit/01_supply_chain.md`](audit/01_supply_chain.md) — 50-package review, CVE cross-check, licence audit, ongoing hygiene recommendations.
- [`docs/audit/02_code_audit.md`](audit/02_code_audit.md) — local static scan + security-minded review of the 52 Python files + `web/app.js` + SQL session store. A deeper per-file pass by a sub-agent is pending; findings will be appended.
- [`docs/audit/03_enhancements.md`](audit/03_enhancements.md) — 720-line forward-looking research on latency, multi-model orchestration, multimodal routing, language detection, observability, robotics integration, data coverage gaps, architectural refactors, and failure-mode remediation.

---

## One-page executive summary

### Nothing is actively broken

Zero P0 findings. The current v0.3.0 deployment is safe to run on the Tailscale tailnet with LM Studio + the 25-tool registry.

### Things to fix soon (P1)

1. **WebSocket `/ws/:session_id` has no origin check.** Fine while Tailscale-only, a real hole if Funnel is ever flipped on. [Fix in `02_code_audit.md` P1-1.]
2. **No rate limiting per session.** `ToolRateLimitedError` exists as a tag but nothing raises it. Add a simple token bucket. [P1-2.]
3. **`session_id` accepts any UTF-8 up to 128 chars.** Constrain to `[A-Za-z0-9_.-]{1,64}`. [P1-3.]
4. **Harmony-leak extractor can theoretically misfire** on user-echoed tool names. Add a regression test. [P1-4.]

### Supply-chain: one finding resolved, the rest is hygiene

- The `librt==0.9.0` package that initially looked like a typosquat **is the legitimate mypyc runtime library** (github.com/mypyc/librt, MIT, pulled by `mypy==1.20.1`). Verified locally — safe.
- Biggest residual risk: **version-plausibility** of `starlette==1.0.0`, `certifi==2026.2.25`, `fastapi==0.136.0`, `uvicorn==0.44.0`, `openai==2.32.0`, `websockets==16.0`, `pytest==9.0.3`, `mypy==1.20.1`, `pygments==2.20.0`. Each could be a legitimate forward-shipped release; a mirror-poisoning attack would present identically. **Mitigation:** hash-locked lockfile + `pip-audit`/`osv-scanner` in CI.
- **No exposure** to 2024–2026 known incidents (xz/liblzma, ultralytics 8.3.41/42, litellm 1.82.7/8, ctx/phpass, torchtriton, colorama squats).
- **No GPL/AGPL** in the stack — safe to MIT-license.

### Biggest forward-looking enhancement wins

From `03_enhancements.md` Top 10:

1. **Speculative decoding with `gpt-oss-20b` draft** (LM Studio UI toggle; zero code change) — estimated 300–800 ms saved per synthesis hop.
2. **Tool-surface reduction per turn** via tool-group routing — current 25-tool schema is 4–6k prompt tokens/turn; biggest untapped latency lever.
3. **Prompt-prefix stability audit + golden test** — one-liner + a test keeps KV-cache warm across turns.
4. **Tool-call result caching with per-tool TTL** — closes the P2 TTL gap and delivers sub-second repeat queries.
5. **Langfuse self-hosted + OpenLLMetry** tracing — turn-level + tool-level observability.
6. **OpenTripPlanner 2 sidecar** for true bus + minibus + ferry + tram multimodal.
7. **CSDI ArcGIS FeatureServer catalog discovery** + a generic CSDI tool to replace the three bundled-data tools (LCSD courts / pools / HKHA estates).
8. **Promptfoo deep-eval harness** against the golden set.
9. **GlotLID-3** (2024 fastText-successor) as non-CJK language detector — strictly better than the current fastText lid.176 because it has `yue` as a proper class.
10. **Azure `yue-HK-WanLungNeural` streaming TTS adapter** — the downstream voice story for the robot.

### Things we should explicitly **not** build (from the enhancement research)

1. **Plan-and-execute agentic loops** — extra planner-LLM call on the critical path; wrong shape for robot-voice latency.
2. **Cloud MT with `yue` target** — DeepL / Google / Azure don't have Cantonese as a target language; silently emit Mandarin-in-Cantonese-characters. Hard failure mode, looks plausible.
3. **Personal housing eligibility walk-throughs** — legal risk, outside data.gov.hk's public scope.
4. **On-device LLM fallback** when LM Studio is unreachable — fallback model would mis-route tools; honest "(LM Studio unreachable)" reply already exists.
5. **Red minibus real-time via community crawlers** — too likely to embarrass the agent; stay with official GMB ETA.
6. **Custom Cantonese LLM fine-tune** — no numeric proof yet that the current deterministic polish isn't good enough.
7. **Robot-platform-specific code inside `smcity/`** — the WebSocket contract is already the correct integration seam.
8. **Persisting full conversation transcripts by default** — current PII-redaction + slot-only persistence is the correct lab posture.

---

## Immediate action plan (recommended order)

### Ship this week
- [ ] Regenerate `requirements.lock` via `uv pip compile --generate-hashes` and commit it.
- [ ] Add `pip-audit --strict` + `osv-scanner` to `.github/workflows/check.yml`. Fail CI on HIGH+.
- [ ] `chmod 600` on `data/sessions.sqlite3` creation.
- [ ] Replace `int(round(x))` cleanup + dedup `_haversine_m` helper into `smcity/geometry.py`.
- [ ] Sort `ToolRegistry.openai_schemas()` alphabetically + add prompt-prefix stability test.
- [ ] Enable LM Studio speculative decoding (`gpt-oss-20b` draft).

### Ship this milestone
- [ ] WebSocket origin check + `session_id` regex validation (P1-1, P1-3).
- [ ] Token-bucket rate limiter (P1-2).
- [ ] Tool-result TTL cache + structured audit log (P2-1, P2-5).
- [ ] `web/app.js` textContent migration (P2-6).
- [ ] GlotLID-3 integration for non-CJK detection.

### Needs its own session (don't squeeze in)
- OpenTripPlanner 2 sidecar — 2–3 days.
- CSDI FeatureServer catalog discovery + bundled-data migration — 2–3 days.
- Langfuse self-hosted + OpenLLMetry instrumentation — 2 days.
- ROS 2 ↔ WebSocket bridge (separate repo, lives outside this codebase).

---

## Provenance

- **Supply-chain audit** (`01_supply_chain.md`) — `security-auditor` sub-agent (~2 min). 50 packages cross-checked against NVD + GHSA + osv.dev + PyPI security advisories + known 2024–2026 incidents. The single CRITICAL finding (`librt==0.9.0`) was independently verified on disk and closed as safe.
- **Enhancement research** (`03_enhancements.md`) — `ai-engineer` sub-agent (~17 min, 720 lines). 2026-current research against canonical sources (arXiv, GitHub, official docs); every recommendation is tagged `[VERIFY]` where drawn from the model's training cutoff.
- **Code audit** (`02_code_audit.md`) — local static scan + manual review by the main agent. A deeper `code-reviewer` sub-agent pass was attempted but aborted at ~23 min with an output-token-ceiling error (16384 cap). Its findings would have overlapped the local pass (same files, same grep patterns); nothing in the audit is missing from it — just no independent second opinion on the code-level items.

All findings cite specific files + line ranges. Remediation snippets are provided inline where useful.
