// Demo console · strip board. All figures come from real calls (see demo/server.py).
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = { customers: new Map(), sessions: new Map(), order: [], active: null, views: {} };

const ENGINE = {
  hosted: { short: "HOSTED · MAF", label: "Option 1 · Hosted agent (MAF)" },
  filesearch: { short: "FILE SEARCH", label: "Option 2 · Prompt agent + File Search" },
};
const QTYPE = {
  greeting: "Greeting",
  general_plus_customer: "General + customer",
  image_only: "Image fact",
  cross_doc: "Cross-document",
  single_doc: "Single document",
  canary: "Internal case file",
  negative_other_customer: "Other customer",
};
const SOURCE = { dms: "DMS", bank: "Bank" };
const TOOL_LABEL = {
  search_general_conditions: "General documents (AI Search)",
  search_customer_documents: "Customer documents (session index)",
  file_search_call: "File Search (session vector store)",
  azure_ai_search_call: "General documents (AI Search)",
  azure_ai_search_call_output: null,
};

// ---------- formatting ----------
const nf = new Intl.NumberFormat("en-US");
const fmtS = (v) => {
  if (v == null || Number.isNaN(Number(v))) return "—";
  const d = v < 10 ? 2 : 1;
  return new Intl.NumberFormat("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }).format(v) + " s";
};
const fmtMs = (ms) => (ms == null ? "—" : fmtS(ms / 1000));
const fmtN = (v) => (v == null ? "—" : nf.format(v));
const fmtMB = (b) => new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 }).format(b / 1e6) + " MB";
const pct = (a, b) => (a == null || !b ? null : Math.round((100 * a) / b));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const cleanTitle = (t, cid) => t.replace(` - ${cid}`, "");

// ---------- answer: minimal markdown + citations ----------
const CITE = /\[((?:CLI|GEN)-[A-Z0-9-]+[^\]\n]{0,60})\]/g;
function inline(s) {
  return s
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(CITE, (_, inner) =>
      inner
        .split(/; ?(?=(?:CLI|GEN)-)/)
        .map((c) => `<span class="cite">${c}</span>`)
        .join(""),
    );
}
function renderAnswer(text, caret = false) {
  const lines = esc(String(text || "").replace(/【[^】]*】/g, "")).split("\n");
  let html = "";
  let inList = false;
  for (const raw of lines) {
    const line = raw.trimEnd();
    const item = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)$/);
    if (item) {
      if (!inList) {
        html += "<ul>";
        inList = true;
      }
      html += `<li>${inline(item[1])}</li>`;
      continue;
    }
    if (inList) {
      html += "</ul>";
      inList = false;
    }
    if (!line.trim()) continue;
    const heading = line.match(/^#{1,4}\s+(.*)$/);
    html += heading ? `<p class="h">${inline(heading[1])}</p>` : `<p>${inline(line)}</p>`;
  }
  html += inList ? "</ul>" : "";
  if (caret) {
    const mark = '<span class="caret" aria-hidden="true"></span>';
    html = /<\/(p|li)>(<\/ul>)?$/.test(html) ? html.replace(/(<\/(?:p|li)>)((?:<\/ul>)?)$/, `${mark}$1$2`) : html + mark;
  }
  return html;
}
const plain = (s) => String(s ?? "").replace(/\*\*/g, "").replace(/【[^】]*】/g, "");

// ---------- API ----------
async function api(path, options = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

async function streamTurn(key, message, onEvent) {
  const res = await fetch(`/api/sessions/${key}/turns`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let match;
    while ((match = buffer.match(/\r?\n\r?\n/))) {
      const chunk = buffer.slice(0, match.index);
      buffer = buffer.slice(match.index + match[0].length);
      let event = "message";
      let data = "";
      for (const line of chunk.split(/\r?\n/)) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (data) onEvent(event, JSON.parse(data));
    }
  }
}

// ---------- derived session values ----------
function prepSeconds(s) {
  const init = s.server?.init?.timings_ms?.total ?? s.server?.filesearch?.timings_ms?.total;
  if (init != null) return init / 1000;
  const line = (s.progress || []).find((p) => /ready in/.test(p.text));
  const m = line?.text.match(/ready in ([\d.,]+) s/);
  return m ? Number(m[1]) : null;
}
function routeText(s) {
  if (s.engine === "filesearch") return "Vector store per session";
  const actual = s.server?.init?.route;
  if (s.route) return `${s.route === "cag" ? "CAG" : "Index"} · forced`;
  if (actual) return `${actual === "cag" ? "CAG" : "Index"} · automatic`;
  return "Automatic";
}
const greetingTtft = (s) => s.turns[0]?.ttft_s ?? null;
const lastTtft = (s) => (s.turns.length > 1 ? s.turns[s.turns.length - 1].ttft_s : null);

// ---------- bay ----------
const printed = new Set();
function stripHTML(s) {
  const c = state.customers.get(s.customer_id);
  const busy = s.busy || s.printing;
  const isNew = !printed.has(s.key);
  const numClass = (v) => (v == null ? "num pending" : "num");
  return `
  <li class="strip strip--${s.engine}${state.active === s.key ? " is-active" : ""}${busy ? " is-busy" : ""}${s.printing ? " is-printing" : ""}${isNew ? " is-new" : ""}" data-key="${s.key}">
    <button class="strip-hit" type="button" ${s.printing ? "disabled" : ""} aria-pressed="${state.active === s.key}"
      aria-label="Session for ${esc(c?.full_name)} with ${esc(ENGINE[s.engine].label)}">
      <span class="row-top">
        <span class="box box-call"><span class="k">${esc(s.customer_id)}</span><span class="v">${esc(c?.full_name)}</span></span>
        <span class="box box-type"><span class="k">${ENGINE[s.engine].short}</span><span class="v">${esc(routeText(s))}</span></span>
      </span>
      <span class="row-bottom">
        <span class="box"><span class="lbl">Setup</span><span class="${numClass(s.setup_s)}">${s.printing ? "…" : fmtS(s.setup_s)}</span></span>
        <span class="box"><span class="lbl">Preparation</span><span class="${numClass(prepSeconds(s))}">${fmtS(prepSeconds(s))}</span></span>
        <span class="box"><span class="lbl">Greeting</span><span class="${numClass(greetingTtft(s))}">${fmtS(greetingTtft(s))}</span></span>
        <span class="box"><span class="lbl">Last</span><span class="${numClass(lastTtft(s))}">${fmtS(lastTtft(s))}</span></span>
      </span>
    </button>
  </li>`;
}
function renderStrips() {
  const focused = document.activeElement?.closest?.(".strip")?.dataset.key;
  const list = state.order.map((k) => state.sessions.get(k)).filter(Boolean);
  $("#strips").innerHTML = list.map(stripHTML).join("");
  for (const s of list) printed.add(s.key);
  if (focused) $(`.strip[data-key="${focused}"] .strip-hit`)?.focus();
  $("#count").textContent = list.length ? `${list.length} ${list.length === 1 ? "open" : "open"}` : "";
}

// ---------- workstation ----------
function workingStripHTML(s) {
  const c = state.customers.get(s.customer_id);
  const products = c.products.map((p) => p.plan).join(" · ");
  const idLine =
    s.engine === "filesearch"
      ? s.vector_store_id
        ? `vector store ${s.vector_store_id}`
        : "vector store: created on greeting"
      : `session ${s.agent_session_id?.slice(0, 18)}…`;
  const numBox = (label, v) => `<span class="box"><span class="lbl">${label}</span><span class="num${v == null ? " pending" : ""}">${fmtS(v)}</span></span>`;
  return `
    <div class="ws-top">
      <span class="box ws-who"><span class="k">${esc(s.customer_id)}</span><span class="name">${esc(c.full_name)}</span><span class="v">${esc(products)}</span></span>
      <span class="box ws-engine"><span class="k">${ENGINE[s.engine].short}</span><span class="v">${esc(routeText(s))}</span><span class="v id">${esc(idLine)}</span></span>
      <span class="actions"><button class="btn btn--secondary" type="button" data-action="close">Close session</button></span>
    </div>
    <div class="ws-bottom">
      ${numBox("Setup", s.setup_s)}
      ${numBox("Preparation", prepSeconds(s))}
      ${numBox("Greeting · first token", greetingTtft(s))}
      ${numBox("Last · first token", lastTtft(s))}
    </div>`;
}

function toolsHTML(turn) {
  const chips = [];
  for (const t of turn.toolEvents || (turn.tools || []).map((name) => ({ name, done: true }))) {
    const label = TOOL_LABEL[t.name] === undefined ? t.name : TOOL_LABEL[t.name];
    if (!label) continue;
    chips.push(`<span class="tool${t.done ? "" : " is-running"}">${esc(label)}${t.done ? "" : " …"}</span>`);
  }
  return chips.length ? `<div class="tools" aria-label="Tools used">${chips.join("")}</div>` : "";
}

function retrievedText(turn, cid) {
  const by = turn.retrieved_by_customer;
  if (!by) return null;
  const entries = Object.entries(by);
  if (!entries.length) return { text: "none", clean: true };
  return { text: entries.map(([k, v]) => `${k} ×${v}`).join(" · "), clean: entries.every(([k]) => k === cid) };
}

function metricsHTML(s, turn, index) {
  const cells = [];
  const cell = (label, value, extra = "") => cells.push(`<div class="${extra}"><dt>${label}</dt><dd>${value}</dd></div>`);
  cell("First token", fmtS(turn.ttft_s));
  cell("Total", fmtS(turn.total_s));
  if (turn.ingesta_s != null) cell("Vector store ingestion", fmtS(turn.ingesta_s));
  const server = s.engine !== "filesearch" ? s.server?.turns?.[index] : null;
  if (server) cell("First token, server", fmtMs(server.ttft_ms));
  cell("Input", `${fmtN(turn.input_tokens)} <small>tk</small>`);
  const p = pct(turn.cached_tokens, turn.input_tokens);
  cell("Cached", `${fmtN(turn.cached_tokens)}${p != null ? ` <small>${p}%</small>` : ""}`);
  if (server?.prefetch_ms != null) cell("General prefetch", fmtMs(server.prefetch_ms));
  const r = retrievedText(turn, s.customer_id);
  if (r) cell("Retrieved files", esc(r.text), `wide ${r.clean ? "is-clean" : "is-mixed"}`);
  return `<dl class="metrics">${cells.join("")}</dl>`;
}

const prepLine = (p) =>
  `<li><span class="t">+${fmtS(p.t)}</span><span>${esc(p.text.replace(/^▸\s*/, ""))}</span></li>`;
const prepHTML = (turn) =>
  turn.progress?.length
    ? `<div class="prep"><div class="prep-title">Session preparation · time since “${esc(turn.message)}”</div><ol>${turn.progress.map(prepLine).join("")}</ol></div>`
    : "";
function answerInner(turn, live) {
  if (turn.answer) return renderAnswer(turn.answer, live);
  if (!live) return "";
  const waiting = turn.progress?.length ? "Preparing documentation…" : "Waiting for the first response…";
  return `<p class="pending">${waiting}<span class="caret" aria-hidden="true"></span></p>`;
}
const errorHTML = (turn) =>
  turn.error
    ? `<div class="turn-error" role="alert"><b>Error:</b> ${esc(typeof turn.error === "string" ? turn.error : JSON.stringify(turn.error))}. Send the message again or open a new session.</div>`
    : "";

function turnHTML(s, turn, index, live = false) {
  return `
  <article class="turn${live ? " is-live" : ""}"${live ? ' id="live-turn"' : ""}>
    <div class="msg msg--user"><span class="who">Customer</span><div class="body">${esc(turn.message)}</div></div>
    ${prepHTML(turn)}
    ${toolsHTML(turn)}
    <div class="msg msg--agent"><span class="who">Agent</span><div class="answer">${answerInner(turn, live)}</div></div>
    ${errorHTML(turn)}
    ${live ? "" : metricsHTML(s, turn, index)}
  </article>`;
}

function renderTranscript(s) {
  const target = $("#transcript");
  const parts = s.turns.map((t, i) => turnHTML(s, t, i));
  if (s.live) parts.push(turnHTML(s, s.live, s.turns.length, true));
  target.innerHTML = parts.join("");
  target.scrollTop = target.scrollHeight;
}

// Update the current turn in chunks so already-painted content is not reanimated.
function updateLive(s) {
  const target = $("#transcript");
  const live = s.live;
  const nearBottom = target.scrollHeight - target.scrollTop - target.clientHeight < 140;
  let node = $("#live-turn");
  if (!node) {
    target.insertAdjacentHTML("beforeend", turnHTML(s, live, s.turns.length, true));
    node = $("#live-turn");
  } else {
    const ol = $(".prep ol", node);
    if (!ol && live.progress.length) $(".msg--user", node).insertAdjacentHTML("afterend", prepHTML(live));
    else if (ol) for (let i = ol.children.length; i < live.progress.length; i++) ol.insertAdjacentHTML("beforeend", prepLine(live.progress[i]));
    const tools = toolsHTML(live);
    const current = $(".tools", node);
    if (current) {
      if (current.outerHTML !== tools) current.outerHTML = tools;
    } else if (tools) $(".msg--agent", node).insertAdjacentHTML("beforebegin", tools);
    $(".answer", node).innerHTML = answerInner(live, true);
    const err = $(".turn-error", node);
    if (live.error && !err) node.insertAdjacentHTML("beforeend", errorHTML(live));
  }
  if (nearBottom) target.scrollTop = target.scrollHeight;
}

let liveFrame = 0;
function scheduleLive(s, live) {
  if (liveFrame) return;
  liveFrame = requestAnimationFrame(() => {
    liveFrame = 0;
    if (state.active !== s.key || s.live !== live) return;
    updateLive(s);
  });
}

function renderComposer(s) {
  const c = state.customers.get(s.customer_id);
  const busy = s.busy;
  const items = s.turns.length || s.live ? c.suggested : [{ type: "greeting", question: "Good morning" }];
  $("#suggestions").innerHTML = items
    .map(
      (q) =>
        `<button type="button" class="suggestion${q.type === "greeting" ? " suggestion--greeting" : ""}" data-question="${esc(q.question)}" title="${esc(q.question)}" ${busy ? "disabled" : ""}><b>${esc(QTYPE[q.type] || q.type)}</b><span>${esc(q.question)}</span></button>`,
    )
    .join("");
  const msg = $("#msg");
  msg.disabled = busy;
  $("#send-btn").disabled = busy;
  $("#send-btn").textContent = busy ? "Responding…" : s.turns.length ? "Send" : "Greet";
  if (!s.turns.length && !s.live && !msg.value) msg.value = "Good morning";
}

function renderWork() {
  const s = state.sessions.get(state.active);
  const has = Boolean(s && !s.printing);
  $("#work-empty").hidden = has;
  $("#working-strip").hidden = !has;
  $("#transcript").hidden = !has;
  $("#composer").hidden = !has;
  if (!has) return;
  const ws = $("#working-strip");
  ws.className = `working-strip strip--${s.engine}${s.busy ? " is-busy" : ""}`;
  ws.innerHTML = workingStripHTML(s);
  renderTranscript(s);
  renderComposer(s);
}

// ---------- case file ----------
function waterfallHTML(rows, total) {
  const scale = (v) => `${Math.max(0, Math.min(100, (100 * v) / total))}%`;
  return `<div class="waterfall">${rows
    .map(
      (r) =>
        `<div class="wf-row"><span class="wf-label">${r.label}</span><span class="wf-track"><span class="wf-bar ${r.cls || ""}" style="left:${scale(r.start)};width:${scale(r.dur)}"></span></span><span class="wf-val">${fmtMs(r.dur)}</span></div>`,
    )
    .join("")}</div>`;
}

function serverPrepHTML(s) {
  if (s.engine === "filesearch") {
    const fs = s.server?.filesearch;
    if (!fs) return `<p class="empty">Measured on greeting: PDF retrieval, upload, and indexing in a new vector store.</p>`;
    const t = fs.timings_ms || {};
    const rows = [];
    if (t.source_dms != null) rows.push({ label: "Document management system", start: 0, dur: t.source_dms, cls: "alt" });
    if (t.source_bank != null) rows.push({ label: "Bank channel", start: 0, dur: t.source_bank, cls: "alt" });
    rows.push({ label: "Retrieval", start: 0, dur: t.retrieval });
    rows.push({ label: "Vector store", start: t.retrieval, dur: t.vector_store });
    rows.push({ label: "Total", start: 0, dur: t.total, cls: "total" });
    return `${waterfallHTML(rows, t.total)}
      <dl class="facts">
        <dt>Vector store</dt><dd class="data">${esc(fs.vector_store_id)}</dd>
        <dt>Indexed files</dt><dd class="data">${fmtN(fs.stats?.completed)} / ${fmtN(fs.stats?.docs)}${fs.stats?.failed ? ` · ${fs.stats.failed} failed` : ""}</dd>
        <dt>Volume</dt><dd class="data">${fs.stats?.bytes ? fmtMB(fs.stats.bytes) : "—"}</dd>
        <dt>Images</dt><dd>File Search indexes only PDF text</dd>
      </dl>`;
  }
  const init = s.server?.init;
  if (!init) return `<p class="empty">Measured on greeting: the agent retrieves and prepares documentation in its own sandbox.</p>`;
  const t = init.timings_ms;
  const rows = [
    { label: "Document management system", start: 0, dur: t.source_dms, cls: "alt" },
    { label: "Bank channel", start: 0, dur: t.source_bank, cls: "alt" },
    { label: "Preparation", start: t.retrieval, dur: t.extraction },
  ];
  if (init.route === "index") rows.push({ label: "Indexing", start: t.retrieval + t.extraction, dur: t.preparation - t.extraction });
  rows.push({ label: "Total", start: 0, dur: t.total, cls: "total" });
  const tot = init.totals || {};
  const last = s.server?.turns?.[s.server.turns.length - 1];
  const lastUsage = last?.usage || {};
  return `${waterfallHTML(rows, t.total)}
    <dl class="facts">
      <dt>Route</dt><dd>${init.route === "cag" ? "CAG · full context with cache" : "Index · AI Search filtered by session"}</dd>
      <dt>Format</dt><dd>${tot.format === "prepared" ? "per-page text + embedded images" : esc(tot.format)}${tot.pdf_docs ? ` · ${tot.pdf_docs} full PDFs` : ""}</dd>
      <dt>Documents</dt><dd class="data">${fmtN(tot.docs)} · ${fmtN(tot.pages)} pages · ${tot.bytes ? fmtMB(tot.bytes) : "—"}</dd>
      <dt>Images</dt><dd class="data">${fmtN(tot.images)}</dd>
      <dt>Estimated tokens</dt><dd class="data">${fmtN(tot.est_tokens)}</dd>
      ${init.route === "index" ? `<dt>Chunks</dt><dd class="data">${fmtN(init.chunks_indexed)}</dd>` : ""}
      <dt>Cache key</dt><dd class="data">${esc(init.cache_key)}</dd>
    </dl>
    ${
      last
        ? `<h2 class="sub">Last server-side turn</h2>
    <dl class="facts">
      <dt>First token</dt><dd class="data">${fmtMs(last.ttft_ms)}</dd>
      <dt>Total</dt><dd class="data">${fmtMs(last.total_ms)}</dd>
      <dt>Input</dt><dd class="data">${fmtN(lastUsage.input_token_count)} tk · cache ${fmtN(lastUsage.cache_read_input_token_count)}</dd>
      <dt>Cache write</dt><dd class="data">${fmtN(lastUsage.cache_creation_input_token_count)} tk</dd>
      ${last.prefetch_ms != null ? `<dt>General prefetch</dt><dd class="data">${fmtMs(last.prefetch_ms)} · ${esc((last.prefetch_sources || []).join(", "))}</dd>` : ""}
    </dl>`
        : ""
    }`;
}

function renderDossier() {
  const target = $("#dossier");
  const s = state.sessions.get(state.active);
  if (!s || s.printing) {
    target.innerHTML = `<p class="empty">The case file and server-side preparation appear when you select a session.</p>`;
    return;
  }
  const c = state.customers.get(s.customer_id);
  const limit = state.views.allDocs ? c.docs.length : 14;
  const docs = c.docs
    .slice(0, limit)
    .map(
      (d) =>
        `<li><span class="doc-type">${esc(d.doc_type)}</span><span class="doc-title" title="${esc(d.title)}">${esc(cleanTitle(d.title, c.customer_id))}</span><span class="doc-meta">${d.pages} pp · ${esc(SOURCE[d.source] || d.source)}</span></li>`,
    )
    .join("");
  const more =
    c.docs.length > 14
      ? `<button type="button" class="btn btn--secondary more-btn" data-action="toggle-docs">${state.views.allDocs ? "Show less" : `Show all ${c.docs.length} documents`}</button>`
      : "";
  target.innerHTML = `
    <section>
      <h2>Case file</h2>
      <p class="sum">${c.n_docs} documents · ${c.pages} pages · ${fmtMB(c.bytes)} · ${c.docs.filter((d) => d.images).length} with image-only data</p>
      <ul class="docs">${docs}</ul>
      ${more}
    </section>
    <section>
      <h2>Server-side preparation</h2>
      ${serverPrepHTML(s)}
    </section>`;
}

function renderAll() {
  renderStrips();
  renderWork();
  renderDossier();
}

// ---------- actions ----------
function select(key) {
  state.active = key;
  state.views.allDocs = false;
  $("#msg").value = "";
  renderAll();
}

async function openSession(ev) {
  ev.preventDefault();
  const customer_id = $("#customer").value;
  const engine = $("input[name=engine]:checked").value;
  const route = engine === "filesearch" ? null : $("#route").value || null;
  const tmpKey = `tmp-${Date.now()}`;
  const placeholder = { key: tmpKey, customer_id, engine, route, turns: [], progress: [], server: {}, printing: true };
  state.sessions.set(tmpKey, placeholder);
  state.order.unshift(tmpKey);
  $("#open-error").hidden = true;
  const btn = $("#open-btn");
  btn.setAttribute("aria-busy", "true");
  btn.disabled = true;
  btn.textContent = "Opening session…";
  renderStrips();
  try {
    const s = await api("/api/sessions", { method: "POST", body: JSON.stringify({ customer_id, engine, route }) });
    state.sessions.delete(tmpKey);
    state.order = state.order.map((k) => (k === tmpKey ? s.key : k));
    state.sessions.set(s.key, s);
    printed.add(s.key);
    select(s.key);
    $("#send-btn").focus();
  } catch (err) {
    state.sessions.delete(tmpKey);
    state.order = state.order.filter((k) => k !== tmpKey);
    const box = $("#open-error");
    box.textContent = `Could not open the session: ${err.message}. Check the Azure session (az login) and try again.`;
    box.hidden = false;
    renderStrips();
  } finally {
    btn.removeAttribute("aria-busy");
    btn.disabled = false;
    btn.textContent = "Open session";
  }
}

async function send(message) {
  const s = state.sessions.get(state.active);
  if (!s || s.busy || !message.trim()) return;
  const live = { message: message.trim(), answer: "", progress: [], toolEvents: [] };
  s.busy = true;
  s.live = live;
  $("#msg").value = "";
  renderAll();
  let finished = false;
  const settle = (turn) => {
    if (finished) return;
    finished = true;
    s.turns.push(turn);
    if (s.live === live) {
      s.live = null;
      s.busy = false;
    }
    if (state.active === s.key) renderAll();
    else renderStrips();
  };
  const closedTools = () => live.toolEvents.map((t) => ({ ...t, done: true }));
  try {
    await streamTurn(s.key, live.message, (event, data) => {
      if (event === "server") {
        s.server = data;
        if (data.filesearch) s.vector_store_id = data.filesearch.vector_store_id;
        renderStrips();
        if (state.active === s.key) {
          renderDossier();
          if (!s.live) renderWork();
        }
        return;
      }
      if (finished) return;
      if (event === "progress") {
        live.progress.push(data);
        s.progress = live.progress;
        renderStrips();
      } else if (event === "delta") {
        live.answer += data.text;
      } else if (event === "tool") {
        if (data.phase === "start") live.toolEvents.push({ name: data.name, done: false });
        else {
          const t = [...live.toolEvents].reverse().find((x) => x.name === data.name && !x.done);
          if (t) t.done = true;
        }
      } else if (event === "done") {
        settle({ ...data, toolEvents: closedTools() });
        return;
      } else if (event === "error") {
        live.error = data.message;
      }
      scheduleLive(s, live);
    });
  } catch (err) {
    live.error = err.message;
  } finally {
    if (!finished) settle({ ...live, toolEvents: closedTools() });
  }
}

async function closeActive() {
  const s = state.sessions.get(state.active);
  if (!s) return;
  const btn = $("[data-action=close]");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Closing…";
  }
  try {
    await api(`/api/sessions/${s.key}`, { method: "DELETE" });
  } catch {
    /* The session may have expired: remove it from the bay anyway. */
  }
  state.sessions.delete(s.key);
  state.order = state.order.filter((k) => k !== s.key);
  state.active = state.order.find((k) => !state.sessions.get(k)?.printing) || null;
  renderAll();
}

// ---------- sheet views ----------
const SCENARIOS = {
  "H1-H2 hosted CAG": "Three concurrent hosted sessions on the CAG route. Each asks about its own data, another customer, and instruction injection.",
  "H3 hosted index": "Two sessions on the index route: chunks from several sessions in the same AI Search index, filtered by session on the server.",
  "H3 index (structure)": "Direct AI Search query: the session_id filter returns only chunks for that session customer.",
  "F1-F2 File Search per session": "One vector store per session, two concurrent sessions; another-customer and injection questions.",
  "F3 wrong vector store": "Simulated programming error: a new customer conversation receives ANOTHER customer's vector store.",
  "F4 shared without filter": "A single vector store with files from two customers and no filter.",
  "F5 shared with customer_id filter": "The same shared store with a customer_id attribute filter defined in the agent with template {{customer_id}}.",
};
function verdictClass(v) {
  if (v === "OK") return "ok";
  if (/^(FAIL|LEAK|MIX)/.test(v)) return "bad";
  return "neutral";
}
async function renderIsolation() {
  const target = $("#isolation");
  target.innerHTML = `<h1>Customer isolation</h1><p class="lead">Loading the latest test run…</p>`;
  try {
    const data = await api("/api/isolation");
    const groups = new Map();
    for (const r of data.rows) {
      if (!groups.has(r.scenario)) groups.set(r.scenario, []);
      groups.get(r.scenario).push(r);
    }
    const body = [...groups.entries()]
      .map(([name, rows]) => {
        const head = `<tr class="group"><td colspan="6">${esc(name)}<small>${esc(SCENARIOS[name] || "")}</small></td></tr>`;
        return (
          head +
          rows
            .map((r) => {
              const rec = r.retrieved_by_customer ? Object.entries(r.retrieved_by_customer).map(([k, v]) => `${k}: ${v}`).join(" · ") || "none" : "—";
              return `<tr><td class="data">${esc(r.customer)}</td><td>${esc(r.test)}</td><td><span class="verdict verdict--${verdictClass(r.result)}">${esc(r.result)}</span></td><td class="data">${esc((r.leaks || []).join(", ") || "—")}</td><td class="data">${esc(rec)}</td><td class="excerpt">${esc(plain(r.answer))}</td></tr>`;
            })
            .join("")
        );
      })
      .join("");
    target.innerHTML = `
      <h1>Customer isolation</h1>
      <p class="lead">Results measured by <code>scripts/test_isolation.py</code>. Each answer is checked for any other-customer data (internal case file, name, policies, address, national ID) and, in File Search, which customer owns the files retrieved by the tool.</p>
      <ul class="findings">${(data.findings || []).map((f) => `<li class="is-${esc(f.tone)}"><b>${esc(f.title)}</b>${esc(f.body)}</li>`).join("")}</ul>
      <div class="table-wrap"><table>
        <thead><tr><th>Customer</th><th>Test</th><th>Result</th><th>Other-customer data in answer</th><th>Retrieved files</th><th>Answer (excerpt)</th></tr></thead>
        <tbody>${body}</tbody>
      </table></div>
      <p class="source">Source: results/${esc(data.file || "—")}</p>`;
  } catch (err) {
    target.innerHTML = `<h1>Customer isolation</h1><p class="turn-error">Could not load results: ${esc(err.message)}. Run <code>scripts/test_isolation.py</code> and reload.</p>`;
  }
}

async function renderBenchmark() {
  const target = $("#benchmark");
  target.innerHTML = `<h1>Measurements</h1><p class="lead">Loading…</p>`;
  try {
    const rows = await api("/api/benchmark");
    const tr = rows
      .map(
        (r) => `<tr>
          <td class="data">${esc(r.customer_id)}</td>
          <td>${esc(ENGINE[r.engine]?.label || r.engine)}</td>
          <td>${esc(r.route || "—")}</td>
          <td class="data">${r.ok}/${r.questions}</td>
          <td class="data">${fmtS(r.ttft_median_s)}</td>
          <td class="data">${fmtS(r.ttft_p90_s)}</td>
          <td class="data">${fmtS(r.greeting_ttft_s)}</td>
          <td class="data">${fmtS(r.setup_s)}</td>
          <td class="data">${fmtN(r.input_tokens_max)}</td>
          <td class="data">${r.cached_ratio != null ? Math.round(100 * r.cached_ratio) + "%" : "—"}</td>
          <td class="excerpt">${(r.failed || []).map((f) => `${esc(f.qid)} (${esc(QTYPE[f.type] || f.type)})`).join(", ") || "—"}</td>
          <td class="data">${esc(r.file)}</td>
        </tr>`,
      )
      .join("");
    target.innerHTML = `
      <h1>Measurements</h1>
      <p class="lead">Scripted conversations with known ground-truth questions (<code>scripts/converse.py</code>): greeting and questions of each type. Times are first-token times, measured in the client from Spain against Sweden Central.</p>
      <div class="table-wrap"><table>
        <thead><tr><th>Customer</th><th>Engine</th><th>Route</th><th>Correct</th><th>First token, median</th><th>First token, p90</th><th>Greeting</th><th>Session setup</th><th>Max input (tk)</th><th>Cached input</th><th>Failures</th><th>File</th></tr></thead>
        <tbody>${tr}</tbody>
      </table></div>`;
  } catch (err) {
    target.innerHTML = `<h1>Measurements</h1><p class="turn-error">Could not load measurements: ${esc(err.message)}.</p>`;
  }
}

function showView(name) {
  for (const tab of $$(".views [role=tab]")) {
    const on = tab.dataset.view === name;
    tab.setAttribute("aria-selected", String(on));
    tab.tabIndex = on ? 0 : -1;
  }
  for (const v of ["sessions", "isolation", "measurements"]) $(`#view-${v}`).hidden = v !== name;
  if (name === "isolation") renderIsolation();
  if (name === "measurements") renderBenchmark();
}

// ---------- startup ----------
function syncCustomerHint() {
  const c = state.customers.get($("#customer").value);
  if (!c) return;
  const products = c.products.map((p) => p.plan).join(" · ");
  $("#customer-hint").textContent = `${c.n_docs} documents · ${c.pages} pages · ${fmtMB(c.bytes)} · ${products}`;
}
function syncRoute() {
  const engine = $("input[name=engine]:checked").value;
  $("#route").disabled = engine === "filesearch";
  $("#route-field").title = engine === "filesearch" ? "File Search always uses one vector store per session" : "";
}

async function boot() {
  const customers = await api("/api/customers");
  for (const c of customers) state.customers.set(c.customer_id, c);
  $("#customer").innerHTML = customers
    .map((c) => `<option value="${c.customer_id}">${c.customer_id} · ${esc(c.full_name)} · ${c.n_docs} docs</option>`)
    .join("");
  syncCustomerHint();
  for (const s of await api("/api/sessions")) {
    state.sessions.set(s.key, s);
    state.order.push(s.key);
  }
  state.active = state.order[0] || null;
  renderAll();
}

$("#new-session").addEventListener("submit", openSession);
$("#customer").addEventListener("change", syncCustomerHint);
$$("input[name=engine]").forEach((r) => r.addEventListener("change", syncRoute));
$("#strips").addEventListener("click", (ev) => {
  const li = ev.target.closest(".strip");
  if (li && !li.classList.contains("is-printing")) select(li.dataset.key);
});
$("#composer").addEventListener("submit", (ev) => {
  ev.preventDefault();
  send($("#msg").value);
});
$("#msg").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
    ev.preventDefault();
    send($("#msg").value);
  }
});
$("#suggestions").addEventListener("click", (ev) => {
  const b = ev.target.closest(".suggestion");
  if (b && !b.disabled) send(b.dataset.question);
});
document.addEventListener("click", (ev) => {
  const action = ev.target.closest("[data-action]")?.dataset.action;
  if (action === "close") closeActive();
  if (action === "toggle-docs") {
    state.views.allDocs = !state.views.allDocs;
    renderDossier();
  }
});
$(".views").addEventListener("click", (ev) => {
  const tab = ev.target.closest("[role=tab]");
  if (tab) showView(tab.dataset.view);
});
$(".views").addEventListener("keydown", (ev) => {
  if (!["ArrowLeft", "ArrowRight"].includes(ev.key)) return;
  const tabs = $$(".views [role=tab]");
  const i = tabs.indexOf(document.activeElement);
  const next = tabs[(i + (ev.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
  next.focus();
  showView(next.dataset.view);
});
syncRoute();
boot().catch((err) => {
  $("#work-empty").insertAdjacentHTML("beforeend", `<p class="turn-error">Could not load the console: ${esc(err.message)}.</p>`);
});
