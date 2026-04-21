# Open Questions — awaiting answers before coding

Numbers match the questions from the kickoff conversation. I'll keep my preferred default next to each so we can move if you don't have an opinion yet.

### Blockers for Phase 0

- **Q1 · Git repo URL.** You said one is created — share the remote so the scaffold lands on main. *Default if silent:* stay local-only; wire remote later.
- **Q5 · Tailscale exposure.** Tailscale Serve (internal HTTPS) vs Funnel (public). *Default:* Serve-only.
- **Q11 · PII posture.** v0 public-info only vs any user PII? *Default:* public-info only; PII redaction at ingress; `meta.forget_me` tool.

### Blockers for Phase 1 acceptance

- **Q4 · Chat UI.** Minimal FastAPI + vanilla JS with tool trace / Next.js+AI SDK / bring-your-own. *Default:* minimal FastAPI + vanilla JS in v0; clean service API underneath so your lab's future UI plugs in.
- **Q7 · Location grounding from the platform.** Does the robotics layer push current lat/lng into every request, or do users type? *Default:* user-types in v0; wire a `context.user_location` slot the platform can fill later.
- **Q12 · Golden evaluation set.** You or the lab provide 20 golden queries, or I seed it and you approve? *Default:* I draft 20 (6 Cantonese, 6 繁體, 4 EN, 4 code-switched); you stamp.

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
