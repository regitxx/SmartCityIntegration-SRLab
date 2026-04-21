# Open Questions — awaiting answers before coding

Numbers match the questions from the kickoff conversation. I'll keep my preferred default next to each so we can move if you don't have an opinion yet.

### Blockers for Phase 0

- **Q1 · Git repo URL.** ✅ **ANSWERED 2026-04-21.** https://github.com/regitxx/SmartCityIntegration-SRLab — docs pushed to `main`.
- **Q5 · Tailscale exposure.** ✅ **ANSWERED 2026-04-21.** Using the Earnest Design Lab tailnet. Default Serve-only (internal HTTPS, no Funnel) unless user flips it on.
- **Q11 · PII posture.** v0 public-info only vs any user PII? *Default:* public-info only; PII redaction at ingress; `meta.forget_me` tool. **Proceeding on default.**

### Blockers for Phase 1 acceptance

- **Q4 · Chat UI.** ✅ **ANSWERED 2026-04-21.** Minimal FastAPI + vanilla JS + WebSocket + SSE, styled in an **"archive underground"** aesthetic (see `docs/architecture/UI_STYLE.md`).
- **Q7 · Location grounding from the platform.** ✅ **ANSWERED 2026-04-21.** v0 is user-types-in-chat. No robot GPS push. `context.user_location` slot is still reserved for when the lab platform wires in later.
- **Q12 · Golden evaluation set — languages.** 🔶 **CLARIFIED 2026-04-21.** data.gov.hk natively serves almost exclusively **EN + 繁體 + 简体** per dataset (rare exceptions on tourism datasets). True "100% coverage" in our agent = native path (EN/繁體/简体) + translation-fallback path for everything else (including Cantonese — not natively supported by data.gov.hk). Golden set v0.1 = 30 queries split **18 native-path + 12 fallback-path** across ≥ 10 languages. I'll draft; user stamps. *Awaiting final ack — user can still tell me to drop the 12 fallback-path queries to Phase 2 if they want a tighter v0.1.*

### Important but deferrable

- **Q2 · Language scope interpretation.** Wire dataset-native `lang=` per source, fallback to translation only for unsupported. *Default:* yes, exactly this, with an explicit coverage matrix.
- **Q3 · Cantonese assist stage.** You OK with a Qwen2.5-7B / YueLLM-7B post-processor for Cantonese surface form? *Default:* yes, starts Phase 2.
- **Q6 · Session memory scope.** Ephemeral only vs persistent per-user. *Default:* ephemeral in v0; persistent behind Q11.
- **Q8 · Housing scope.** Lookups + stats only vs eligibility walk-through. *Default:* lookups + stats; explicit redirect to official form for eligibility.
- **Q9 · Basketball-court routing stack.** OK with ALS + LCSD venue directory + OTP2 / Valhalla? *Default:* yes.
- **Q10 · Budget / cloud constraints.** Is everything strictly local, or are Azure TTS / DeepL / cloud translation on the table? *Default:* strictly local for v0; feature flag cloud TTS/MT in Phase 2 if you want.

### Scope-shape questions (nice to know by Phase 3)

- Anticipated daily query volume + concurrent sessions on the Mac Studio?
- Does the lab already have STT of choice for Cantonese? (Affects upstream contract, not the agent itself.)
- Any integration deadlines from the lab platform you're not seeing yet?
- Who's the "native HK reviewer" for the Cantonese polish eval in Phase 2?

### Reminder

If I don't hear back I move on the defaults and flag the assumption in the commit message + `CHANGELOG.md`. You can always tell me to back out — reversals are cheap in these phases.
