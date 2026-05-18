// archive underground · Phase 1a UI client
// Wires: language selector + WebSocket to /ws/:session_id + live tool_call events + final message render.
// All dynamic text is written via textContent / DOM construction (no innerHTML with user or upstream data).

const SESSION_ID = (() => {
  const key = "smcity.session";
  let id = sessionStorage.getItem(key);
  if (!id) {
    id = (crypto.randomUUID?.() ?? Math.random().toString(16).slice(2)).replace(/-/g, "").slice(0, 12);
    sessionStorage.setItem(key, id);
  }
  return id;
})();

const $ = (sel) => document.querySelector(sel);
const dialogue = $("#dialogue");
const input = $("#input");
const trace = $("#trace");
const langSelect = $("#lang");
$("#session-id").textContent = SESSION_ID;

const STATUS_GLYPH = { ok: "✓", error: "!", timeout: "⏱", rate_limited: "⧖", skipped: "·" };

function hhmmss(d = new Date()) {
  return d.toTimeString().slice(0, 8);
}

// --- DOM builders (textContent only) -------------------------------------

function el(tag, { className, text, attrs } = {}) {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text !== undefined) n.textContent = text;
  if (attrs) for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
}

function chip(label, variant) {
  return el("span", { className: variant ? `chip ${variant}` : "chip", text: label });
}

function addMessage({ who, text, lang, srcFooter }) {
  const msg = el("div", { className: `msg ${who}` });

  const meta = el("div", { className: "meta" });
  const glyph = who === "user" ? "≥" : who === "agent" ? "·" : "*";
  meta.append(
    el("span", { className: "who", text: `${glyph} ${who}` }),
    el("span", { className: "time", text: hhmmss() }),
  );
  if (lang) meta.append(el("span", { className: "lang", text: lang }));

  const body = el("div", { className: "body", text });
  if (lang) body.setAttribute("lang", lang);

  msg.append(meta, body);

  if (srcFooter) {
    const foot = el("div", { className: "footer-src" });
    // srcFooter is a list of {label, variant} chips — never raw HTML.
    for (const item of srcFooter) foot.append(chip(item.label, item.variant));
    msg.append(foot);
  }

  dialogue.append(msg);
  dialogue.scrollTop = dialogue.scrollHeight;
  return msg;
}

let _traceCursor = 0;
let _turnOpen = false;

function resetTrace() {
  trace.replaceChildren();
  _traceCursor = 0;
}

function addTraceStart({ name, args, query_lang }) {
  _traceCursor += 1;
  const li = el("li");
  li.dataset.tool = name;
  li.dataset.idx = String(_traceCursor);
  li.append(
    el("span", { className: "idx", text: `[${String(_traceCursor).padStart(2, "0")}]` }),
    el("span", { className: "name", text: name }),
    el("span", { className: "args", text: args ? JSON.stringify(args) : "" }),
    el("span", {
      className: "status running",
      text: `· running${query_lang ? ` · lang=${query_lang}` : ""}`,
    }),
  );
  trace.append(li);
  trace.scrollTop = trace.scrollHeight;
}

function addTraceResult({ name, status, latency_ms, error }) {
  const open = [...trace.querySelectorAll("li")].reverse().find(
    (li) => li.dataset.tool === name && li.querySelector(".status.running"),
  );
  if (!open) return;
  const glyph = STATUS_GLYPH[status] || "?";
  const statusEl = open.querySelector(".status");
  statusEl.className = `status ${status}`;
  statusEl.textContent = `· ${glyph} ${status} · ${latency_ms} ms${error ? ` · ${error}` : ""}`;
}

function langChip(langInfo) {
  if (!langInfo) return null;
  const { source, primary_lang, translation_applied } = langInfo;
  return translation_applied
    ? chip(`translated: ${primary_lang}`, "translated")
    : chip(`${source}: ${primary_lang}`, "native");
}

// --- WebSocket wiring ------------------------------------------------------
const wsProto = location.protocol === "https:" ? "wss" : "ws";
const ws = new WebSocket(`${wsProto}://${location.host}/ws/${SESSION_ID}`);

ws.addEventListener("open", () => {
  addMessage({ who: "system", text: `connected · session ${SESSION_ID}` });
});

ws.addEventListener("close", () => {
  addMessage({ who: "system", text: "disconnected" });
});

let _draftingNode = null;  // the message element currently being streamed into
let _draftBody = null;     // the .body element we stream tokens into
let _turnStartedAt = 0;

ws.addEventListener("message", (ev) => {
  let msg;
  try { msg = JSON.parse(ev.data); } catch { return; }
  switch (msg.type) {
    case "ready":
      addMessage({ who: "system", text: `ready · ${msg.model} · ${hhmmss(new Date(msg.ts))} HKT` });
      break;
    case "locale_set":
      addMessage({ who: "system", text: `locale → ${msg.locale}` });
      break;
    case "turn.start":
      _turnOpen = true;
      _turnStartedAt = performance.now();
      resetTrace();
      _draftingNode = addMessage({
        who: "agent",
        text: "",
        lang: msg.detected_lang,
      });
      _draftBody = _draftingNode.querySelector(".body");
      if (_draftBody) _draftBody.classList.add("streaming");
      if (msg.fast_path) {
        const meta = _draftingNode.querySelector(".meta");
        if (meta) meta.append(chip(`fast-path: ${msg.fast_path}`));
      }
      break;
    case "tool_call.start":
      addTraceStart(msg);
      break;
    case "tool_call.result":
      addTraceResult(msg);
      break;
    case "turn.llm_first_token":
      if (_draftBody) _draftBody.classList.remove("streaming");
      break;
    case "turn.token":
      if (_draftBody && msg.text) {
        _draftBody.textContent += msg.text;
        dialogue.scrollTop = dialogue.scrollHeight;
      }
      break;
    case "turn.final": {
      _turnOpen = false;
      const d = msg.data;
      if (_draftingNode && _draftBody) {
        // ALWAYS overwrite streamed text with the final cleaned text from
        // the server. The streamed path bypasses the harmony-leak extractor
        // and source-footer rewriter — keeping the streamed text would mean
        // bare-leak tool calls (e.g. `transport.plan_journey json{...}`) and
        // LLM-invented `src:` lines reach the UI. Trust the server's final.
        const finalText = d.text || "";
        if (finalText) _draftBody.textContent = finalText;
        _draftBody.classList.remove("streaming");
        const foot = el("div", { className: "footer-src" });
        const lc = langChip(d.lang);
        if (lc) foot.append(lc);
        if (d.citations?.length) {
          const shorts = d.citations.map((c) => c.tool.split(".").pop()).join(" · ");
          foot.append(chip(`src: ${shorts}`));
        }
        foot.append(chip(`${d.elapsed_ms} ms`));
        _draftingNode.append(foot);
      } else {
        addMessage({ who: "agent", text: d.text, lang: d.lang?.primary_lang });
      }
      _draftingNode = null;
      _draftBody = null;
      break;
    }
    case "error":
      addMessage({ who: "system", text: `error · ${msg.message}` });
      break;
  }
});

// --- input ----------------------------------------------------------------
input.addEventListener("keydown", (ev) => {
  if (ev.key !== "Enter" || ev.shiftKey) return;
  ev.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  if (ws.readyState !== WebSocket.OPEN) {
    addMessage({ who: "system", text: "not connected" });
    return;
  }
  const locale_override = langSelect.value === "auto" ? null : langSelect.value;
  addMessage({ who: "user", text, lang: locale_override || undefined });
  ws.send(JSON.stringify({ type: "turn", text, locale_override }));
  input.value = "";
});

// --- language selector ----------------------------------------------------
langSelect.addEventListener("change", () => {
  if (ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: "set_locale", locale: langSelect.value }));
});

// keyboard shortcut: cmd/ctrl + shift + L → reset to auto
window.addEventListener("keydown", (ev) => {
  if ((ev.metaKey || ev.ctrlKey) && ev.shiftKey && (ev.key === "l" || ev.key === "L")) {
    ev.preventDefault();
    langSelect.value = "auto";
    langSelect.dispatchEvent(new Event("change"));
  }
});
