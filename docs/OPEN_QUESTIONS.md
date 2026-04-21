# Open Questions — kickoff decisions

Numbers match the questions from the kickoff conversation. All Phase 0-2c blockers are resolved and shipped in v0.1.0. New questions live at the bottom of the file.

### Blockers for Phase 0 — all resolved

- **Q1 · Git repo URL.** ✅ https://github.com/regitxx/SmartCityIntegration-SRLab — 9 commits + `v0.1.0` tag on `main`.
- **Q5 · Tailscale exposure.** ✅ Earnest Design Lab tailnet, Tailscale Serve-only. Funnel stays opt-in. See `docs/DEPLOY.md`.
- **Q11 · PII posture.** ✅ Public-info-only. `redact_pii()` at ingress (HK phone + HKID regexes). `meta.forget_me` + ephemeral 24 h SQLite TTL.

### Blockers for Phase 1 acceptance — all resolved

- **Q4 · Chat UI.** ✅ Minimal vanilla JS + WebSocket + archive-underground style, fully shipped. See `web/` and `docs/architecture/UI_STYLE.md`.
- **Q7 · Location grounding from the platform.** ✅ v0 is user-types-in-chat. `context.user_location` slot reserved for when the robot layer pushes GPS — see `docs/PROTOCOL.md`.
- **Q12 · Golden evaluation set.** ✅ Native-path 18 + fallback-path 12 + phase1b bucket 7 = 37 queries across 10+ languages. See `tests/golden/v0_1_queries.json`.

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
