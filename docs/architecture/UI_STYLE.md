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
- **Language selector** — see next section.

## Language selector

Fixed component in the top-right of the header, left of the session id. Default value is `auto` — the language router decides per turn. Manually selecting a language **locks** both the input-interpretation hint and the output language until the user either changes it or resets to `auto`.

### Behaviour

- **auto** (default): router runs per turn; response footer shows the detected language and whether translation was applied.
- **explicit pick** (e.g. `yue`): router is bypassed; the picked language is treated as authoritative for this and all subsequent turns in the session. The chip in the footer reads `forced: <lang>` instead of `detected:`.
- Picking a language data.gov.hk does not natively serve (e.g. `jpn`) turns on the translation-fallback path for every tool call until the selector changes.
- Reset to `auto` from a keyboard shortcut (`⌘ + ⇧ + L` / `Ctrl + Shift + L`) or by clicking the label `[lang:]` itself.
- Current selection persists per-session (carried in `SessionSlots.locale`) and survives reconnect.

### Visual

```
LAB_SRL · SMART_CITY_INTEGRATION · v0          [lang: auto ▾]    [session: 6f3a…]
```

- Rendered as `[lang: <value> ▾]`. Value is lowercase ISO (`auto`, `yue`, `zh-Hant`, `zh-Hans`, `en`, `ja`, `ko`, `fr`, `de`, `es`, `th`, `tl`, `id`, `vi`, `…more`).
- Same hairline / UPPERCASE letter-spaced chrome as the rest of the header. No background fill; accent colour on hover and focus.
- Dropdown panel: monospace, hairline rules between items, each row `<code>   <display name>` e.g. `yue   廣東話`. Always shown in the item's own script (廣東話, 日本語, 한국어) — not transliterated.
- Selected state: a small `·` before the code (`· yue`) — no checkmark, no flourish.

### Option list (v0.1)

Priority-first ordering. The `…more` entry opens a second tier covering the remaining fastText-supported languages.

| Code | Display | Path |
|---|---|---|
| `auto` | auto · 自動 | router decides |
| `yue` | 廣東話 | **priority** · fallback via translation (no native support on data.gov.hk) |
| `zh-Hant` | 繁體中文 | native (most datasets) |
| `zh-Hans` | 简体中文 | native (many datasets) |
| `en` | English | native (universal) |
| `ja` | 日本語 | fallback |
| `ko` | 한국어 | fallback |
| `fr` | Français | fallback |
| `de` | Deutsch | fallback |
| `es` | Español | fallback |
| `th` | ไทย | fallback |
| `tl` | Tagalog | fallback |
| `id` | Bahasa Indonesia | fallback |
| `vi` | Tiếng Việt | fallback |
| `…more` | — | opens full fastText list, grouped by script |

### Accessibility

- Native `<select>` for fidelity with assistive tech (styled via CSS; `appearance: none`).
- `aria-label="chat language"`; each `<option>` has `lang="<code>"` so screen readers pronounce the display name correctly.
- Keyboard: `Tab` reaches it, `Enter`/`Space` opens, arrows navigate, `Esc` closes.

### Server contract

- `POST /turn` accepts an optional `locale_override: "<code>" | "auto"` field in the body.
- WebSocket `set_locale` event: `{"type":"set_locale","locale":"yue"}` — server acks with `{"type":"locale_set","locale":"yue","at":"<iso>"}`.
- Server state: `SessionSlots.locale.source = "user"` when user-picked; `"auto"` when auto.
- When `locale_override` changes mid-session, the agent **acknowledges** the switch in the next reply ("switched to 廣東話") in the newly selected language, then continues normally.

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
