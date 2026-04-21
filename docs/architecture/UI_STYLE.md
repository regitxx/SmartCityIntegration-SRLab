# UI Style — Archive Underground

**Version:** 2026-04-21 · v0.1
**Scope:** v0 dev chat UI only — the lab's future robotics platform provides its own surface.

The v0 chat UI is minimal, text-forward, and wears an "archive underground" aesthetic. The point is to look like a working-log terminal from a design lab's archive, not a consumer product. The tool trace sidebar is the star — seeing the agent think is the feature.

---

## Aesthetic direction

- **Mood:** archive.org · early-web BBS · Berkeley Mono portfolio · brutalist zine · dockets + index cards · late-'90s CRT terminals.
- **NOT:** rounded-corners SaaS chatbot, glassmorphism, gradient hero, emoji reactions, AI-flavored sparkles.
- **One-line brief:** "a service terminal you'd find tucked into a research lab in Sham Shui Po at 2 AM."

## Typography

- **Primary:** a monospace — preferred `Berkeley Mono` (if we have the license), otherwise `JetBrains Mono` or `IBM Plex Mono`. Fallback stack: `ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`.
- **CJK:** pair with `Noto Sans HK` / `Noto Serif HK` for 繁體 and Cantonese; `Noto Sans SC` for 简体. Never substitute Japanese CJK fonts for HK text.
- **Sizing:** base 14 px, line-height 1.55. Metadata 12 px. Title caps at 18 px — no bigger.
- **Weight:** mostly regular (400); bold (600) sparingly for speaker labels; italic for system notes.
- **Case:** headers and chrome in **UPPERCASE with letter-spacing 0.06 em** (shipping-label feel). Body text in natural case.

## Colour

Two palettes — pick one via `prefers-color-scheme`, dark is the default.

### Dark (default)

| Role | Hex | Use |
|---|---|---|
| Background | `#0B0B0A` | page base — near-black, slight warmth |
| Surface | `#13120F` | message cards, tool-trace rail |
| Hairline | `#262420` | 1 px rules between sections |
| Body text | `#E6E1D1` | ivory, not pure white |
| Muted text | `#7C776B` | timestamps, source footers |
| Accent | `#C9A24A` | amber — single accent; used for speaker labels, focus ring, active tool |
| Alert | `#A5432A` | rust — warnings (typhoon, API down) |
| Affirm | `#5F7048` | moss green — successful tool result |

### Light

| Role | Hex |
|---|---|
| Background | `#ECE6D3` (newsprint cream) |
| Surface | `#F6F1DE` |
| Hairline | `#C5BDA5` |
| Body text | `#141310` |
| Muted text | `#6A6455` |
| Accent | `#8A5A1A` (burnt amber) |
| Alert | `#7A2A15` |
| Affirm | `#3F4E2A` |

Single accent per palette. No gradients. No drop shadows.

## Layout

```
┌─ LAB_SRL · SMART_CITY_INTEGRATION · v0 ─────────────────────── [session: 6f3a…] ┐
│                                                                                 │
│  ≥ user · 14:03:22 · yue                                                        │
│  我喺上環，點樣去沙田？                                                         │
│                                                                                 │
│  · agent · 14:03:24 · yue                                                       │
│  由上環去沙田，最快係搭 MTR 港島線轉東鐵線…                                    │
│  src: mtr_next_train · kmb_eta · hko_warnsum · fetched 14:03:23                 │
│                                                                                 │
├── tool trace ───────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  [1] geo.address_lookup          { q: "上環" }            ok · 184 ms            │
│  [2] transport.find_stops_near   { lat, lng }             ok · 92 ms             │
│  [3] transport.get_mtr_next_...  { sta: "SHW" }           ok · 210 ms            │
│  [4] context.get_active_warnings { }                      ok · 140 ms            │
│                                                                                 │
└─────────────────────────────────────────────────────────────────────────────────┘
```

- Two stacked panes (single column ≤ 900 px, two-column above): **dialogue** (top, ~60%) and **tool trace** (bottom, ~40%). On wide viewports the tool trace can pin to the right instead.
- Header is a single 40 px row with the project label left, session id right. Monospace, UPPERCASE, hairline border below.
- Speaker markers — `≥ user` / `· agent` — mimic prompt + log-line glyphs. Time in 24 h local (Asia/Hong_Kong). Language tag right after (`yue`, `zho-Hant`, `jpn`, …).
- **No bubbles.** Messages are flush-left blocks with a subtle left-side rule in the accent color for `agent` turns.
- Footer on every agent message: `src: <tool_names> · fetched HH:MM:SS · translated from <lang>` when applicable.

## Components (minimal set)

- **Message** — speaker marker, timestamp, language tag, body, optional source footer.
- **Tool trace row** — `[index] tool_name { args_inline } status · latency_ms`. Click to expand raw JSON.
- **Language coverage chip** (inline at response bottom) — `native: yue ⁄ 繁體` or `translated: jpn → 繁體`.
- **Warning strip** — sits at the top of the dialogue pane only when a HKO warning is active. Full-width, alert colour, text UPPERCASE (`TYPHOON SIGNAL 8 · HKO 14:00`).
- **Input** — single-row text field, 1 px hairline top, monospace, prompt glyph `≥ ` as placeholder. Enter sends. No send button unless keyboard is unavailable.

## Motion

- Near-zero. Fades `80 ms` max, ease-out. No bouncy transitions. No typing indicator with animated dots — replace with `≥ agent · drafting…` text.
- **Optional scanline overlay** (dark mode only) at 6% opacity, disable-able via a `?plain` query param and `prefers-reduced-motion`.

## Chrome / details

- Hairlines are 1 px `hairline` colour — never use `#fff` dividers.
- Focus rings: 2 px accent, no box-shadow glow.
- Scrollbars: thin (`6px`), accent thumb on surface track.
- Copy buttons appear on hover only, as `[copy]` text — no icon.
- Favicon: the `≥` glyph on the surface colour, rendered as SVG inline.

## Accessibility

- Contrast ratios target WCAG AA on body (`#E6E1D1` on `#0B0B0A` = ~15:1) and AA-large on muted text.
- Keyboard-first: every action reachable without mouse. Tab order: input → last agent message → tool trace (top→bottom).
- `aria-live="polite"` on the dialogue pane; `aria-live="off"` on the tool trace so screen readers don't flood.
- `prefers-reduced-motion` disables the scanline overlay and any fades.

## What this UI is explicitly NOT

- Not a product UI. It's a lab console.
- Not a chatbot persona. No avatar, no name, no "Hi, I'm Aria ✨" welcome message. The first line is always a neutral status row: `ready · lm-studio/openai-gpt-oss-120b · 14:02:11 HKT`.
- Not themed per session. One aesthetic, two palettes (dark / light), done.

## Reference implementations to draw from

- archive.org search results pages
- Berkeley Mono designer portfolios
- are.na block view
- old BBS log viewers (ANSI clean, not neon)
- Jan Tschichold's "Die neue Typographie" as a general discipline for chrome.
