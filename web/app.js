// archive underground · Phase 0 UI shell
// Wires: language selector, WebSocket to /ws/:session_id, minimal message rendering.

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

function hhmmss(d = new Date()) {
  return d.toTimeString().slice(0, 8);
}

function addMessage({ who, text, lang, srcFooter }) {
  const msg = document.createElement("div");
  msg.className = `msg ${who}`;
  const meta = document.createElement("div");
  meta.className = "meta";
  const glyph = who === "user" ? "≥" : who === "agent" ? "·" : "*";
  meta.innerHTML = `<span class="who">${glyph} ${who}</span><span class="time">${hhmmss()}</span>${lang ? `<span class="lang">${lang}</span>` : ""}`;
  const body = document.createElement("div");
  body.className = "body";
  if (lang) body.setAttribute("lang", lang.replace("yue", "yue").replace("zh-Hant", "zh-Hant"));
  body.textContent = text;
  msg.append(meta, body);
  if (srcFooter) {
    const foot = document.createElement("div");
    foot.className = "footer-src";
    foot.innerHTML = srcFooter;
    msg.append(foot);
  }
  dialogue.append(msg);
  dialogue.scrollTop = dialogue.scrollHeight;
  return msg;
}

function addTraceEntry(entry) {
  const li = document.createElement("li");
  const { index, name, args, status, latency_ms } = entry;
  li.innerHTML = `<span class="idx">[${String(index).padStart(2, "0")}]</span><span class="name">${name}</span><span class="args">${args ? JSON.stringify(args) : ""}</span><span class="status ${status}">· ${status}</span><span class="latency">· ${latency_ms} ms</span>`;
  trace.append(li);
}

function renderLangChip(langInfo) {
  if (!langInfo) return "";
  const { source, primary_lang, translation_applied } = langInfo;
  const chip = translation_applied
    ? `<span class="chip translated">translated: ${primary_lang}</span>`
    : `<span class="chip native">${source}: ${primary_lang}</span>`;
  return chip;
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

ws.addEventListener("message", (ev) => {
  let msg;
  try { msg = JSON.parse(ev.data); } catch { return; }
  switch (msg.type) {
    case "ready":
      addMessage({
        who: "system",
        text: `ready · ${msg.model} · ${hhmmss(new Date(msg.ts))} HKT`,
      });
      break;
    case "locale_set":
      addMessage({ who: "system", text: `locale → ${msg.locale}` });
      break;
    case "turn.start":
      _draftingNode = addMessage({ who: "agent", text: "drafting…" });
      break;
    case "turn.final":
      if (_draftingNode) {
        _draftingNode.remove();
        _draftingNode = null;
      }
      const d = msg.data;
      addMessage({
        who: "agent",
        text: d.text,
        lang: d.lang?.primary_lang,
        srcFooter:
          renderLangChip(d.lang) +
          (d.citations?.length
            ? `<span class="chip">src: ${d.citations.map((c) => c.tool).join(" · ")}</span>`
            : "") +
          `<span class="chip">${d.elapsed_ms} ms</span>`,
      });
      (d.tool_trace || []).forEach(addTraceEntry);
      break;
    case "error":
      addMessage({ who: "system", text: `error · ${msg.message}` });
      break;
  }
});

let _draftingNode = null;

// --- input -----------------------------------------------------------------
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

// --- language selector -----------------------------------------------------
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
