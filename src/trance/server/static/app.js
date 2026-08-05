"use strict";

const state = {
  session: null,
  roles: {},
  providers: [],
  presets: [],
  workspace: "",
  agents: [],
  toolsets: [],
  orchestrator: null,
  kinds: {},
  ws: null,
  events: [],
  draftSteps: [],
};

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

function toast(message) {
  const box = $("toast");
  box.textContent = message;
  box.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => box.classList.remove("show"), 6000);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    toast(`${res.status}: ${detail}`);
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

async function copyText(text) {
  try {
    // Only available in secure contexts — over http:// on a remote host it is
    // undefined, which is precisely how this UI is usually reached.
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_) { /* fall through */ }
  const box = document.createElement("textarea");
  box.value = text;
  box.setAttribute("readonly", "");
  box.style.cssText = "position:fixed;top:0;left:0;opacity:0";
  document.body.append(box);
  box.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
  box.remove();
  return ok;
}

function copyButton(getText, label = "copy") {
  const btn = el("button", "copy-btn", label);
  btn.onclick = async (e) => {
    e.preventDefault();
    e.stopPropagation();     // don't toggle the <details> it sits in
    const ok = await copyText(typeof getText === "function" ? getText() : getText);
    btn.textContent = ok ? "copied" : "select & copy";
    if (!ok) toast("Clipboard blocked by the browser — the text is selected, press Ctrl+C.");
    setTimeout(() => { btn.textContent = label; }, 1600);
  };
  return btn;
}

function confirmDialog(title, detail) {
  return new Promise((resolve) => {
    const back = el("div", "modal open");
    const card = el("div", "modal-card confirm-card");
    card.append(el("h2", null, title));
    if (detail) card.append(el("p", "muted small", detail));
    const row = el("div", "row");
    const cancel = el("button", null, "Cancel");
    const go = el("button", "primary danger", "Delete");
    const done = (value) => { back.remove(); resolve(value); };
    cancel.onclick = () => done(false);
    go.onclick = () => done(true);
    back.onclick = (e) => { if (e.target === back) done(false); };
    row.append(cancel, go);
    card.append(row);
    back.append(card);
    document.body.append(back);
    go.focus();
  });
}

function show(screen) {
  document.querySelectorAll(".screen").forEach((s) => s.classList.remove("active"));
  $(`screen-${screen}`).classList.add("active");
}

/* ─────────────────────────────── home ─────────────────────────────── */

// The directory tracks the project name until the user edits it by hand.
let dirTouched = false;
$("new-dir").addEventListener("input", () => { dirTouched = true; });
$("new-name").addEventListener("input", () => {
  if (dirTouched || !state.workspace) return;
  const slug = $("new-name").value.trim().toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "");
  $("new-dir").value = slug ? `${state.workspace}/${slug}` : state.workspace;
  clearTimeout(pathCheckTimer);
  pathCheckTimer = setTimeout(checkNewPath, 350);
});

async function loadWorkspace() {
  const w = await api("/api/workspace");
  state.workspace = w.workspace;
  $("workspace-path").textContent = w.workspace;
  const stateEl = $("state-path");
  if (stateEl) stateEl.textContent = w.state_dir;
  if (!w.writable) {
    $("dir-check").textContent = `${w.workspace} is not writable — pick another workspace.`;
    $("dir-check").className = "check-result bad";
  }
  // Prefill both fields so creating a project is one click.
  if (!$("new-name").value && !dirTouched) {
    $("new-name").value = w.suggested_name;
    $("new-dir").value = w.suggested_dir;
  }
}

async function loadHome() {
  const sessions = await api("/api/sessions");
  const list = $("session-list");
  list.innerHTML = "";
  if (!sessions.length) list.append(el("p", "muted small", "No sessions yet."));
  sessions.forEach((s) => {
    const row = el("div");
    const left = el("div", "grow");
    left.append(el("div", null, s.name));
    left.append(el("div", "muted small", `${s.project_dir} · ${s.status}`));

    const right = el("div", "row");
    right.append(el("span", "badge", s.status));
    const del = el("button", "icon-btn danger", "🗑");
    del.title = "Delete this session";
    del.onclick = async (e) => {
      e.stopPropagation();   // the row itself opens the session
      const ok = await confirmDialog(
        `Delete session “${s.name}”?`,
        "Its chat, flow, and trace are removed. Files the agents wrote to " +
        `${s.project_dir} are left on disk.`);
      if (!ok) return;
      await api(`/api/sessions/${s.id}`, { method: "DELETE" });
      if (state.session?.id === s.id) { state.session = null; renderSessionBar(); }
      toast(`Deleted “${s.name}”.`);
      loadHome();
    };
    right.append(del);

    row.append(left, right);
    row.onclick = () => openSession(s.id);
    list.append(row);
  });
}

// Check the path as it is typed — a typo here otherwise only surfaced deep
// into a run, after the whole flow had been planned.
let pathCheckTimer;
$("new-dir").addEventListener("input", () => {
  clearTimeout(pathCheckTimer);
  pathCheckTimer = setTimeout(checkNewPath, 350);
});

async function checkNewPath() {
  const dir = $("new-dir").value.trim();
  const out = $("dir-check");
  if (!dir) { out.textContent = ""; out.className = "check-result"; return true; }
  const r = await api("/api/check-path", { method: "POST", body: { project_dir: dir } });
  out.textContent = r.ok ? `will use ${r.path}` : r.error;
  out.className = `check-result ${r.ok ? "ok" : "bad"}`;
  return r.ok;
}

$("create-session").onclick = async () => {
  const name = $("new-name").value.trim() || "untitled";
  const dir = $("new-dir").value.trim();
  if (!dir) return toast("A project directory is required.");
  if (!(await checkNewPath())) return toast("Fix the project directory first.");
  const session = await api("/api/sessions", { method: "POST", body: { name, project_dir: dir } });
  openSession(session.id);
};

/* ────────────────────────────── session ───────────────────────────── */

async function openSession(id) {
  state.session = await api(`/api/sessions/${id}`);
  state.events = [];
  consoleStep = null;
  consoleReset();
  connect(id);
  renderSessionBar();
  renderChat();
  renderFlowEditor();
  show(state.session.status === "planning" ? "plan" : "run");
  if (state.session.status !== "planning") renderRun();
}

function renderSessionBar() {
  const bar = $("session-bar");
  bar.innerHTML = "";
  if (!state.session) return;
  const back = el("button", null, "← sessions");
  back.onclick = () => {
    show("home");
    dirTouched = false;
    $("new-name").value = "";
    $("new-dir").value = "";
    $("dir-check").textContent = "";
    loadWorkspace();
    loadHome();
  };
  bar.append(back, el("span", null, state.session.name),
             el("span", "badge", state.session.status));
  const plan = el("button", null, "Plan");
  plan.onclick = () => show("plan");
  const run = el("button", null, "Run");
  run.onclick = () => { show("run"); renderRun(); };
  bar.append(plan, run);
}

/* ──────────────────────────────── chat ────────────────────────────── */

function renderChat() {
  const log = $("chat-log");
  log.innerHTML = "";
  (state.session?.chat || []).forEach((m) => {
    log.append(el("div", `msg ${m.role}`, m.content));
  });
  log.scrollTop = log.scrollHeight;
}

$("chat-send").onclick = sendChat;
$("chat-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) sendChat();
});

async function sendChat() {
  const box = $("chat-text");
  const text = box.value.trim();
  if (!text || !state.session) return;
  box.value = "";
  const btn = $("chat-send");
  btn.disabled = true;
  btn.textContent = "…";
  state.session.chat.push({ role: "user", content: text });
  renderChat();
  try {
    state.session = await api(`/api/sessions/${state.session.id}/chat`, {
      method: "POST", body: { message: text },
    });
    renderChat();
    renderFlowEditor();
    renderSessionBar();
  } finally {
    btn.disabled = false;
    btn.textContent = "Send";
  }
}

/* ───────────────────────── flow editor (plan) ─────────────────────── */

function renderFlowEditor() {
  state.draftSteps = JSON.parse(JSON.stringify(state.session?.flow?.steps || []));
  const box = $("flow-editor");
  box.innerHTML = "";
  if (!state.draftSteps.length) {
    box.append(el("p", "muted small", "No steps yet — describe the project to the orchestrator, or add one manually."));
  }
  state.draftSteps.forEach((step, index) => box.append(stepCard(step, index)));
}

function stepCard(step, index) {
  const card = el("div", "step-card");
  // Only a step whose agent is mid-flight is locked; a failed or finished one
  // is a plan you may correct, and correcting it re-queues it.
  const editable = !["running", "verifying"].includes(step.status);
  card.draggable = editable;
  card.dataset.index = index;
  const role = state.roles[step.role];
  if (role) card.style.borderLeftColor = role.color;

  const head = el("div", "row");
  head.append(el("span", "badge", `${index + 1}`));

  const roleSelect = el("select");
  Object.keys(state.roles).forEach((name) => {
    const opt = el("option", null, name);
    opt.value = name;
    if (name === step.role) opt.selected = true;
    roleSelect.append(opt);
  });
  roleSelect.disabled = !editable;
  roleSelect.onchange = () => { step.role = roleSelect.value; };

  head.append(roleSelect);
  if (step.status && step.status !== "pending") {
    const tag = el("span", "badge", step.status);
    if (!editable) tag.title = "This step is running — edit it once it settles";
    head.append(tag);
  }

  const remove = el("button", null, "✕");
  remove.disabled = !editable;
  remove.onclick = () => { state.draftSteps.splice(index, 1); redrawEditor(); };
  head.append(remove);

  const task = el("textarea");
  task.value = step.task || "";
  task.disabled = !editable;
  task.oninput = () => { step.task = task.value; };

  // Three separate controls: the reality check, who fixes a failure, and how
  // many times the block may loop before the run is halted.
  const gatesBox = el("div", "gates");
  const drawGates = () => {
    gatesBox.innerHTML = "";

    const row = el("div", "row small");
    const field = (label, node) => {
      const wrap = el("label", "inline-field", label);
      wrap.append(node);
      return wrap;
    };

    const check = el("select", "compact");
    const none = el("option", null, "no check");
    none.value = "";
    check.append(none);
    Object.values(state.roles).filter((r) => r.verifier).forEach((r) => {
      const opt = el("option", null, r.name);
      opt.value = r.name;
      if (r.name === (step.check || "")) opt.selected = true;
      check.append(opt);
    });
    check.disabled = !editable;
    check.title = "Reality check run after the block. PASS moves the flow on.";
    check.onchange = () => { step.check = check.value || null; drawGates(); };

    const fixer = el("select", "compact");
    const self = el("option", null, `${step.role} retries`);
    self.value = "";
    fixer.append(self);
    Object.values(state.roles).filter((r) => r.name !== "orchestrator").forEach((r) => {
      const opt = el("option", null, r.name);
      opt.value = r.name;
      if (r.name === (step.on_fail || "")) opt.selected = true;
      fixer.append(opt);
    });
    fixer.disabled = !editable || !step.check;
    fixer.title = "Who tries to fix a failed check before the block runs again";
    fixer.onchange = () => { step.on_fail = fixer.value || null; drawGates(); };

    const loops = el("input", "compact tiny");
    loops.type = "number";
    loops.min = 1;
    loops.value = step.max_loops ?? 2;
    loops.disabled = !editable || !step.check;
    loops.title = "Loops allowed before the run is halted";
    loops.onchange = () => { step.max_loops = Number(loops.value) || 1; drawGates(); };

    row.append(field("check", check), field("on fail", fixer), field("loops", loops));
    gatesBox.append(row);

    if (step.check) {
      const who = step.on_fail || step.role;
      gatesBox.append(el("div", "loop-note",
        `${step.role} → ${step.check} · pass → next step · ` +
        `fail → ${who} fixes → ${step.role} again · ` +
        `${step.max_loops ?? 2} loop(s), then the run halts`));
    } else {
      gatesBox.append(el("div", "loop-note muted",
        "No check — the flow moves on as soon as this block finishes."));
    }
  };
  drawGates();

  card.append(head, task, gatesBox);

  card.addEventListener("dragstart", (e) => {
    card.classList.add("dragging");
    e.dataTransfer.setData("text/plain", String(index));
  });
  card.addEventListener("dragend", () => card.classList.remove("dragging"));
  card.addEventListener("dragover", (e) => e.preventDefault());
  card.addEventListener("drop", (e) => {
    e.preventDefault();
    const from = Number(e.dataTransfer.getData("text/plain"));
    const to = index;
    if (Number.isNaN(from) || from === to) return;
    const [moved] = state.draftSteps.splice(from, 1);
    state.draftSteps.splice(to, 0, moved);
    redrawEditor();
  });
  return card;
}

function redrawEditor() {
  const box = $("flow-editor");
  box.innerHTML = "";
  state.draftSteps.forEach((step, index) => box.append(stepCard(step, index)));
}

$("add-step").onclick = () => {
  state.draftSteps.push({ role: "backend", task: "", verify_with: null, max_attempts: 2, status: "pending" });
  redrawEditor();
};

$("save-flow").onclick = async () => {
  const flow = await api(`/api/sessions/${state.session.id}/flow`, {
    method: "PUT", body: { steps: state.draftSteps },
  });
  state.session.flow = flow;
  renderFlowEditor();
  const n = (flow.requeued || []).length;
  toast(n ? `Flow saved — ${n} edited step(s) re-queued to run again.` : "Flow saved.");
};

$("start-run").onclick = async () => {
  await api(`/api/sessions/${state.session.id}/flow`, { method: "PUT", body: { steps: state.draftSteps } });
  state.session = await api(`/api/sessions/${state.session.id}/start`, { method: "POST" });
  show("run");
  renderRun();
};

function providerDefaultModel(name) {
  const provider = state.providers.find((p) => p.name === name);
  return provider ? provider.model : (state.providers[0]?.model || "default");
}

/* ─────────────────────────────── run ──────────────────────────────── */

function renderRun() {
  const session = state.session;
  if (!session) return;
  const status = $("run-status");
  status.innerHTML = "";
  const progress = session.progress || {};
  status.append(el("b", null, session.name), el("span", "badge", session.status));
  status.append(el("span", "muted small",
    `${progress.done || 0} done · ${progress.pending || 0} pending · ${progress.failed || 0} failed`));
  if (session.error) status.append(el("span", "badge", session.error));

  renderFlowView();
  paintPaused();
}

$("btn-pause").onclick = async () => {
  await api(`/api/sessions/${state.session.id}/pause`, { method: "POST" });
  state.session.paused = true;
  paintPaused();
  toast("Paused — click a console line to send a note about that action.");
};
$("btn-resume").onclick = async () => {
  await api(`/api/sessions/${state.session.id}/resume`, { method: "POST" });
  state.session.paused = false;
  paintPaused();
};
$("btn-stop").onclick = () => api(`/api/sessions/${state.session.id}/stop`, { method: "POST" });
$("btn-back-plan").onclick = () => { renderFlowEditor(); show("plan"); };

/* ───────────────────────────── websocket ──────────────────────────── */

function connect(sessionId) {
  if (state.ws) { state.ws.onclose = null; state.ws.close(); }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/${sessionId}`);
  state.ws = ws;
  ws.onmessage = (msg) => {
    const event = JSON.parse(msg.data);
    if (event.type === "snapshot") {
      state.session = event.payload;
      paintPaused();
      if (!["running", "paused"].includes(state.session.status)) clearActivity();
      renderRun();
      renderSessionBar();
      return;
    }
    state.events.push(event);
    trackActivity(event);
    consoleAppend(event);
  };
  ws.onclose = () => setTimeout(() => connect(sessionId), 1500);
}

/* ─────────────────────── event log / inspector ────────────────────── */

function renderEvent(event) {
  const details = el("details", "event");
  const summary = el("summary");
  const time = new Date(event.ts).toLocaleTimeString();
  summary.append(el("span", "when", time));

  if (event.agent) {
    const badge = el("span", "badge role", event.agent);
    const role = state.roles[event.agent];
    badge.style.background = role ? role.color : "#565f89";
    summary.append(badge);
  }
  summary.append(el("span", "badge", event.type));
  summary.append(el("span", "headline", headline(event)));
  details.append(summary);
  details.append(body(event));

  if (event.type === "error") details.classList.add("err");
  if (event.type === "tool_call" && event.payload.ok === false) details.classList.add("ok-false");
  if (event.type === "supervision") details.classList.add("ok-false");
  return details;
}

function headline(event) {
  const p = event.payload || {};
  switch (event.type) {
    case "model_call":
      return `${shortModel(p.model)} · round ${p.round} · ${p.summary?.est_tokens ?? "?"} tok in · ` +
             (p.tool_calls?.length ? `${p.tool_calls.length} tool call(s)` : clip(p.response_text, 90));
    case "tool_call":
      return `${p.name}(${Object.values(p.arguments || {}).map((v) => clip(String(v), 28)).join(", ")})` +
             (p.ok === false ? " — refused" : "");
    case "step_started": return `${clip(p.task, 90)} (attempt ${p.attempt})`;
    case "step_finished": return `${clip(p.summary, 70)} · ${(p.files || []).join(", ") || "no files"}`;
    case "step_failed": return p.reason || "failed";
    case "step_retry": return `retrying — ${clip(p.feedback, 80)}`;
    case "verdict": return `${p.verdict} — ${clip(p.detail, 70)}`;
    case "supervision": return p.message;
    case "context_bundle":
      return `${p.stats?.symbols ?? 0} symbols · ~${p.stats?.est_tokens ?? 0} tok (${p.entry || ""})`;
    case "chat": return clip(p.content, 100);
    case "steering": return `“${clip(p.note, 80)}”`;
    case "index": return p.error ? `index error: ${p.error}` : `${p.parsed} parsed · ${p.symbols ?? 0} symbols`;
    case "error": return p.message;
    default: return clip(JSON.stringify(p), 100);
  }
}

function body(event) {
  const box = el("div", "event-body");
  const p = event.payload || {};

  if (event.type === "model_call") {
    const stats = el("div", "stat-row");
    const s = p.summary || {};
    stats.innerHTML =
      `<span>model <b>${p.model || "?"}</b></span>` +
      `<span>messages <b>${s.message_count ?? "?"}</b></span>` +
      `<span>context <b>~${s.est_tokens ?? "?"}</b> tok</span>` +
      `<span>in <b>${p.usage?.prompt_tokens ?? "?"}</b></span>` +
      `<span>out <b>${p.usage?.completion_tokens ?? "?"}</b></span>` +
      `<span>took <b>${p.duration_ms ?? "?"}</b> ms</span>` +
      `<span>finish <b>${p.finish_reason || "?"}</b></span>`;
    box.append(stats);

    // Collapsed by default: the full prompt is usually thousands of tokens.
    const prompt = el("details");
    const promptSummary = el("summary", "muted small",
      `▸ full context sent to the model (${s.message_count ?? 0} messages, ~${s.est_tokens ?? 0} tokens)`);
    promptSummary.append(copyButton(() => (p.messages || [])
      .map((m) => `--- ${m.role} ---\n${m.content || JSON.stringify(m.tool_calls || "", null, 2)}`)
      .join("\n\n"), "copy all"));
    prompt.append(promptSummary);
    (p.messages || []).forEach((m) => {
      const block = el("div", "msg-block");
      block.append(el("div", "msg-role", m.role + (m.name ? ` · ${m.name}` : "")));
      let content = m.content;
      if (!content && m.tool_calls) content = JSON.stringify(m.tool_calls, null, 2);
      block.append(el("pre", "code", content || "(empty)"));
      prompt.append(block);
    });
    box.append(prompt);

    if (p.reasoning) {
      const reasoning = el("details");
      reasoning.append(el("summary", "muted small", "▸ reasoning"));
      reasoning.append(el("pre", "code", p.reasoning));
      box.append(reasoning);
    }
    if (p.response_text) {
      box.append(rowWithCopy("response", p.response_text));
      box.append(el("pre", "code", p.response_text));
    }
    if (p.tool_calls?.length) {
      box.append(el("div", "msg-role", "requested tools"));
      box.append(el("pre", "code", JSON.stringify(p.tool_calls, null, 2)));
    }
    return box;
  }

  if (event.type === "tool_call") {
    box.append(el("div", "msg-role", "arguments"));
    box.append(el("pre", "code", JSON.stringify(p.arguments, null, 2)));
    if (p.remit_violation) {
      box.append(el("div", "msg-role", "remit violation"));
      box.append(el("pre", "code", p.remit_violation));
    }
    box.append(rowWithCopy(`result (~${p.result_tokens} tok)`, p.result || ""));
    box.append(el("pre", "code", p.result || ""));
    return box;
  }

  if (event.type === "context_bundle") {
    if (p.items) {
      const table = p.items.map((i) => `${i.hops}  ${i.include.padEnd(9)} ${i.qualname}`).join("\n");
      box.append(el("div", "msg-role", "symbols in bundle"));
      box.append(el("pre", "code", table));
    }
    if (p.rendered) {
      const full = el("details");
      full.append(el("summary", "muted small", "▸ full curated bundle"));
      full.append(el("pre", "code", p.rendered));
      box.append(full);
    }
    return box;
  }

  if (event.type === "error" || p.traceback) {
    // JSON.stringify turns a traceback into one unreadable line of \n escapes,
    // and that unbroken line is what was blowing out the page width.
    const text = [p.message, p.traceback].filter(Boolean).join("\n\n");
    box.append(rowWithCopy("error", text));
    box.append(el("pre", "code", text));
    return box;
  }

  const dump = JSON.stringify(p, null, 2);
  box.append(rowWithCopy("payload", dump));
  box.append(el("pre", "code", dump));
  return box;
}

function rowWithCopy(label, text) {
  const row = el("div", "row copy-row");
  row.append(el("span", "msg-role", label), copyButton(() => text));
  return row;
}

function shortModel(model) {
  if (!model) return "?";
  const tail = model.split("/").pop();
  return tail.length > 26 ? tail.slice(0, 26) + "…" : tail;
}

function clip(text, n) {
  text = (text || "").replace(/\s+/g, " ").trim();
  return text.length > n ? text.slice(0, n) + "…" : text;
}

/* ─────────────────────────────── boot ─────────────────────────────── */

(async function boot() {
  const info = await api("/api/config");
  state.roles = info.roles;
  state.providers = info.providers;
  state.presets = info.presets;
  state.orchestrator = info.orchestrator;
  state.kinds = info.kinds;
  await loadWorkspace();
  await loadHome();
})();

/* ─────────────────────────── settings modal ───────────────────────── */

$("open-settings").onclick = openSettings;
$("close-settings").onclick = () => $("settings").classList.remove("open");
$("settings").onclick = (e) => { if (e.target.id === "settings") e.currentTarget.classList.remove("open"); };

async function openSettings() {
  $("settings").classList.add("open");
  await renderProviders();
}

async function renderPresets() {
  const { presets } = await api("/api/presets");
  state.presets = presets;
  const box = $("preset-list");
  box.innerHTML = "";
  if (!presets.length) box.append(el("p", "muted small", "No models defined yet."));
  presets.forEach((m) => box.append(presetCard(m, false)));
}

function presetCard(preset, isNew) {
  const card = el("div", "provider-card preset-card");
  const grid = el("div", "provider-grid");

  const name = el("input", "compact");
  name.value = preset.name;
  name.placeholder = "e.g. cheap-tester";
  name.title = "Rename freely — agents using this model are re-pointed automatically";

  const provider = el("select", "compact");
  state.providers.forEach((p) => {
    const opt = el("option", null, `${p.name} (${p.kind})`);
    opt.value = p.name;
    if (p.name === preset.provider) opt.selected = true;
    provider.append(opt);
  });

  const model = el("input", "compact");
  model.value = preset.model || "";
  model.placeholder = "model id";

  const wrap = (label, node) => {
    const l = el("label", null, label);
    l.append(node);
    return l;
  };
  const ctx = el("input", "compact");
  ctx.type = "number";
  ctx.value = preset.context_window || "";
  ctx.placeholder = "inherit provider";
  ctx.title = "Override when the model's window differs from the provider default " +
              "(e.g. Haiku is 200k where Opus is 1M). Wrong values 400 mid-run.";

  grid.append(wrap("Name (agents pick this)", name), wrap("Provider", provider),
              wrap("Model", model), wrap("Context window", ctx));

  const actions = el("div", "row small");
  const save = el("button", "primary", "Save");
  save.onclick = async () => {
    const shortname = name.value.trim();
    if (!shortname) return toast("A name is required.");

    // Rename first, so the settings below land on the new name and every
    // agent referencing the old one is re-pointed server-side.
    let target = shortname;
    if (!isNew && shortname !== preset.name) {
      const result = await api(`/api/presets/${encodeURIComponent(preset.name)}/rename`, {
        method: "POST", body: { name: shortname },
      });
      target = result.name;
      const moved = result.repointed_sessions || [];
      toast(moved.length
        ? `Renamed to “${target}” — re-pointed ${moved.length} session(s).`
        : `Renamed to “${target}”.`);
    }

    await api(`/api/presets/${encodeURIComponent(target)}`, {
      method: "PUT",
      body: {
        provider: provider.value,
        model: model.value.trim(),
        context_window: Number(ctx.value) || 0,
      },
    });
    if (target === preset.name) toast(`Saved model “${target}”.`);
    await renderPresets();
    await refreshConfig();
    if (state.session) state.session = await api(`/api/sessions/${state.session.id}`);
  };
  actions.append(save);

  if (isNew) {
    const cancel = el("button", null, "Cancel");
    cancel.onclick = () => card.remove();
    actions.append(cancel);
  } else {
    const del = el("button", "danger", "Delete");
    del.onclick = async () => {
      const ok = await confirmDialog(
        `Delete model “${preset.name}”?`,
        "Agents using it fall back to the default model.");
      if (!ok) return;
      await api(`/api/presets/${encodeURIComponent(preset.name)}`, { method: "DELETE" });
      await renderPresets();
      await refreshConfig();
    };
    actions.append(del);
  }

  card.append(grid, actions);
  return card;
}

async function renderProviders() {
  const data = await api("/api/providers");
  state.kinds = data.kinds;
  const box = $("provider-list");
  box.innerHTML = "";
  if (!data.providers.length) {
    box.append(el("p", "muted small", "No providers yet — add one below."));
  }
  state.providers = data.providers.filter((p) => p.enabled);
  data.providers.forEach((p) => box.append(providerCard(p, false)));
  await renderPresets();
  renderOrchestratorSettings();
}

function providerCard(provider, isNew) {
  const card = el("div", `provider-card${provider.enabled ? "" : " disabled"}`);
  const head = el("div", "row");

  const kind = el("select", "compact");
  Object.keys(state.kinds).forEach((k) => {
    const opt = el("option", null, state.kinds[k].label);
    opt.value = k;
    if (k === provider.kind) opt.selected = true;
    kind.append(opt);
  });

  const badge = el("span", `kind-badge kind-${provider.kind}`, provider.kind);
  const name = el("input", "compact");
  name.value = provider.name;
  name.placeholder = "shortname";
  name.disabled = !isNew;           // renaming would orphan agent references
  name.title = isNew ? "The handle you attach to agents" : "Shortname is fixed once created";

  head.append(badge, name, kind);

  const enabled = el("label", "row small");
  const toggle = el("input");
  toggle.type = "checkbox";
  toggle.checked = provider.enabled;
  enabled.append(toggle, document.createTextNode(" active"));
  head.append(enabled);

  const grid = el("div", "provider-grid");
  const fields = {};
  const add = (key, label, value, type = "text") => {
    const wrap = el("label", null, label);
    const input = el("input");
    input.type = type;
    input.value = value ?? "";
    wrap.append(input);
    grid.append(wrap);
    fields[key] = input;
  };
  add("base_url", "Base URL", provider.base_url);
  add("model", "Default model", provider.model);
  add("api_key", "API key", "", "password");
  fields.api_key.placeholder = provider.has_key ? "•••• stored (blank = keep)" : "not set";
  add("context_window", "Context window", provider.context_window, "number");

  // Switching kind repopulates the endpoint defaults for that provider type.
  kind.onchange = () => {
    const d = state.kinds[kind.value];
    badge.className = `kind-badge kind-${kind.value}`;
    badge.textContent = kind.value;
    if (!fields.base_url.value || Object.values(state.kinds).some((x) => x.base_url === fields.base_url.value)) {
      fields.base_url.value = d.base_url;
    }
    if (!fields.model.value || Object.values(state.kinds).some((x) => x.model === fields.model.value)) {
      fields.model.value = d.model;
    }
    fields.context_window.value = d.context_window;
    fields.api_key.placeholder = d.needs_key ? "required" : "not needed";
  };

  const actions = el("div", "row small");
  const save = el("button", "primary", "Save");
  const check = el("button", null, "Test");
  const del = el("button", "danger", "Delete");
  const result = el("span", "check-result");

  save.onclick = async () => {
    const shortname = name.value.trim();
    if (!shortname) return toast("A shortname is required.");
    const body = {
      kind: kind.value,
      base_url: fields.base_url.value.trim(),
      model: fields.model.value.trim(),
      context_window: Number(fields.context_window.value) || 0,
      enabled: toggle.checked,
      label: provider.label,
    };
    if (fields.api_key.value) body.api_key = fields.api_key.value;
    await api(`/api/providers/${encodeURIComponent(shortname)}`, { method: "PUT", body });
    toast(`Saved “${shortname}”.`);
    await renderProviders();
    await refreshConfig();
  };

  check.onclick = async () => {
    result.textContent = "testing…";
    result.className = "check-result";
    try {
      const r = await api(`/api/providers/${encodeURIComponent(provider.name)}/check`, { method: "POST" });
      result.textContent = r.ok ? `reachable — ${r.reply}` : r.error;
      result.className = `check-result ${r.ok ? "ok" : "bad"}`;
    } catch (_) {
      result.textContent = "check failed";
      result.className = "check-result bad";
    }
  };

  del.onclick = async () => {
    const ok = await confirmDialog(
      `Delete provider “${provider.name}”?`,
      "Agents still pointing at this shortname fall back to the default provider.");
    if (!ok) return;
    await api(`/api/providers/${encodeURIComponent(provider.name)}`, { method: "DELETE" });
    toast(`Deleted “${provider.name}”.`);
    await renderProviders();
    await refreshConfig();
  };

  const cancel = el("button", null, "Cancel");
  cancel.onclick = () => card.remove();   // discard an unsaved new provider

  actions.append(save);
  actions.append(isNew ? cancel : check);
  if (!isNew) actions.append(del);
  actions.append(result);
  card.append(head, grid, actions);
  return card;
}

$("add-provider").onclick = () => {
  const kind = "anthropic";
  const d = state.kinds[kind];
  $("provider-list").append(providerCard({
    name: "", kind, base_url: d.base_url, model: d.model,
    context_window: d.context_window, enabled: true, has_key: false, label: "",
  }, true));
};

function renderOrchestratorSettings() {
  const box = $("orchestrator-settings");
  box.innerHTML = "";
  const select = el("select", "compact");
  state.presets.forEach((m) => {
    const opt = el("option", null, `${m.name} — ${m.model}`);
    opt.value = m.name;
    if (m.name === state.orchestrator?.preset) opt.selected = true;
    select.append(opt);
  });
  select.onchange = async () => {
    state.orchestrator = await api("/api/config/orchestrator", {
      method: "PUT", body: { preset: select.value },
    });
    toast("Orchestrator updated.");
  };
  const current = el("span", "muted small",
    state.orchestrator ? `→ ${state.orchestrator.model}` : "");
  box.append(select, current);
}

async function refreshConfig() {
  const info = await api("/api/config");
  state.providers = info.providers;
  state.presets = info.presets;
  state.orchestrator = info.orchestrator;
  state.kinds = info.kinds;
}

$("add-preset").onclick = () => {
  const provider = state.providers[0];
  if (!provider) return toast("Add a provider first — a model needs an endpoint.");
  $("preset-list").append(presetCard(
    { name: "", provider: provider.name, model: provider.model }, true));
};

/* ──────────────────────────── agents modal ────────────────────────── */

$("open-agents").onclick = openAgents;
$("close-agents").onclick = () => $("agents").classList.remove("open");
$("agents").onclick = (e) => { if (e.target.id === "agents") e.currentTarget.classList.remove("open"); };

async function openAgents() {
  $("agents").classList.add("open");
  await renderAgents();
}

async function renderAgents() {
  const data = await api("/api/agents");
  state.toolsets = data.toolsets;
  state.agents = data.agents;
  const box = $("agent-list");
  box.innerHTML = "";
  data.agents.forEach((a) => box.append(agentCard(a, false)));
}

const TOOLSET_HELP = {
  files: "read / write / list files inside the remit",
  graph: "look up symbols, callers and callees in the indexed code",
  commands: "run allowlisted commands (pytest, npm, tsc, …)",
};

function agentCard(agent, isNew) {
  const card = el("div", "provider-card agent-card");
  card.style.borderLeftColor = agent.color || "#7aa2f7";

  const head = el("div", "row");
  const name = el("input", "compact");
  name.value = agent.name;
  name.placeholder = "shortname, e.g. dba";
  name.disabled = !isNew;   // flows reference agents by name
  if (!isNew) name.title = "Built into existing flows — name is fixed";

  const title = el("input", "compact");
  title.value = agent.title || "";
  title.placeholder = "Display title";

  const color = el("input", "compact");
  color.type = "color";
  color.value = agent.color || "#7aa2f7";

  head.append(name, title, color);
  if (agent.protected) head.append(el("span", "badge", "built-in"));
  if (agent.verifier) head.append(el("span", "badge", "verifier"));
  if (agent.resolved) {
    head.append(el("span", "muted small",
      `→ ${agent.resolved.model} (${Number(agent.resolved.context_window).toLocaleString()} ctx)`));
  }

  // --- model -----------------------------------------------------------
  const grid = el("div", "provider-grid");
  const wrap = (label, node, hint) => {
    const l = el("label", null, label);
    l.append(node);
    if (hint) l.append(el("span", "hint", hint));
    return l;
  };

  const preset = el("select", "compact");
  const inherit = el("option", null, "default model");
  inherit.value = "";
  preset.append(inherit);
  state.presets.forEach((m) => {
    const opt = el("option", null, `${m.name} — ${m.model}`);
    opt.value = m.name;
    if (m.name === agent.preset) opt.selected = true;
    preset.append(opt);
  });
  grid.append(wrap("Model", preset));

  const description = el("input", "compact");
  description.value = agent.description || "";
  description.placeholder = "what this agent is for";
  grid.append(wrap("Description", description, "Shown to you, and to the orchestrator when it picks a team"));

  // --- permissions -----------------------------------------------------
  const perms = el("div", "perm-box");
  perms.append(el("div", "msg-role", "permissions"));
  const verifierLabel = el("label", "row small");
  const verifierBox = el("input");
  verifierBox.type = "checkbox";
  verifierBox.checked = !!agent.verifier;
  verifierLabel.append(verifierBox, document.createTextNode(" can verify other steps"));
  verifierLabel.title = "Only these agents appear in a step's verifier dropdown";
  perms.append(verifierLabel);
  const toolsetRow = el("div", "row small");
  const boxes = {};
  (state.toolsets || []).forEach((t) => {
    const label = el("label", "row small");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = (agent.toolsets || []).includes(t);
    label.append(cb, document.createTextNode(` ${t}`));
    label.title = TOOLSET_HELP[t] || "";
    toolsetRow.append(label);
    boxes[t] = cb;
  });
  perms.append(toolsetRow);

  // Commands + where they run — only meaningful with the commands toolset.
  const cmdRow = el("div", "provider-grid");
  const commands = el("input", "compact");
  commands.value = (agent.commands || []).join(" ");
  commands.placeholder = "default allowlist";
  commands.title = "Space-separated programs this agent may run. Blank = the built-in default.";
  const workdir = el("input", "compact");
  workdir.value = agent.workdir || "";
  workdir.placeholder = "project root";
  workdir.title = "Directory (relative to the project) that commands run in";
  const wrapField = (label, node, hint) => {
    const l = el("label", null, label);
    l.append(node);
    if (hint) l.append(el("span", "hint", hint));
    return l;
  };
  cmdRow.append(wrapField("Allowed commands", commands, "blank = default allowlist"),
                wrapField("Run commands in", workdir, "blank = project root"));
  perms.append(cmdRow);

  const paths = el("textarea");
  paths.rows = 3;
  paths.value = (agent.paths || []).join("\n");
  paths.placeholder = "backend/**\n*.py";
  perms.append(el("label", "small", "Remit — one glob per line. Writes outside these are refused."));
  perms.append(paths);

  // --- prompt ----------------------------------------------------------
  const promptWrap = el("details");
  promptWrap.append(el("summary", "muted small", "▸ system prompt"));
  const prompt = el("textarea");
  prompt.rows = 10;
  prompt.value = agent.system_prompt || "";
  promptWrap.append(prompt);
  if ((agent.toolsets || []).length && agent.name !== "orchestrator") {
    promptWrap.append(el("p", "hint",
      "If this agent verifies other steps, its prompt must end with a line " +
      "'VERDICT: PASS' or 'VERDICT: FAIL' — that line is what the engine reads."));
  }

  // --- actions ---------------------------------------------------------
  const actions = el("div", "row small");
  const save = el("button", "primary", "Save");
  const result = el("span", "check-result");

  save.onclick = async () => {
    const shortname = name.value.trim();
    if (!shortname) return toast("A shortname is required.");
    const body = {
      title: title.value.trim() || shortname,
      description: description.value.trim(),
      system_prompt: prompt.value,
      paths: paths.value.split("\n").map((p) => p.trim()).filter(Boolean),
      toolsets: Object.keys(boxes).filter((t) => boxes[t].checked),
      verifier: verifierBox.checked,
      commands: commands.value.split(/[\s,]+/).filter(Boolean),
      workdir: workdir.value.trim(),
      preset: preset.value || null,
      color: color.value,
    };
    try {
      await api(`/api/agents/${encodeURIComponent(shortname)}`, { method: "PUT", body });
    } catch (_) { return; }        // api() already surfaced the reason
    result.textContent = "saved";
    result.className = "check-result ok";
    toast(`Saved agent “${shortname}”.`);
    await renderAgents();
    await refreshConfig();
    if (state.session) state.session = await api(`/api/sessions/${state.session.id}`);
  };
  actions.append(save);

  if (isNew) {
    const cancel = el("button", null, "Cancel");
    cancel.onclick = () => card.remove();
    actions.append(cancel);
  } else if (agent.protected) {
    const reset = el("button", null, "Reset to built-in");
    reset.title = "Restore the shipped prompt and permissions, discarding your edits";
    reset.onclick = async () => {
      const ok = await confirmDialog(`Reset “${agent.name}” to its built-in definition?`,
        "Your edits to its prompt, remit, toolsets and model are discarded.");
      if (!ok) return;
      await api(`/api/agents/${encodeURIComponent(agent.name)}/reset`, { method: "POST" });
      toast(`Reset “${agent.name}”.`);
      await renderAgents();
    };
    actions.append(reset);
  } else {
    const del = el("button", "danger", "Delete");
    del.onclick = async () => {
      const ok = await confirmDialog(`Delete agent type “${agent.name}”?`,
        "Flows that name it will fail to run until you repoint them.");
      if (!ok) return;
      await api(`/api/agents/${encodeURIComponent(agent.name)}`, { method: "DELETE" });
      await renderAgents();
      await refreshConfig();
    };
    actions.append(del);
  }
  actions.append(result);

  card.append(head, grid, perms, promptWrap, actions);
  return card;
}

$("add-agent").onclick = () => {
  $("agent-list").prepend(agentCard({
    name: "", title: "", description: "", system_prompt: "",
    paths: [], toolsets: ["files", "graph"], preset: null, color: "#7aa2f7",
    protected: false,
  }, true));
};

/* ───────────────────── pipeline: scroll + step detail ─────────────── */

$("close-step").onclick = () => $("step-modal").classList.remove("open");
$("step-modal").onclick = (e) => { if (e.target.id === "step-modal") e.currentTarget.classList.remove("open"); };
$("compact-done").onchange = () => renderFlowView();

const TERMINAL = new Set(["done", "failed", "skipped", "blocked"]);

function renderFlowView() {
  const box = $("flow-view");
  box.innerHTML = "";
  const steps = state.session?.flow?.steps || [];
  const compact = $("compact-done").checked;

  const p = state.session?.progress || {};
  $("flow-progress").textContent =
    `${p.done || 0} done · ${p.running || 0} running · ${p.pending || 0} pending` +
    `${p.failed ? ` · ${p.failed} failed` : ""}${p.blocked ? ` · ${p.blocked} blocked` : ""}`;

  steps.forEach((step, index) => {
    if (index > 0) box.append(el("div", `flow-connector${step.verify_with ? " loop" : ""}`));
    const finished = TERMINAL.has(step.status);
    const node = el("div", `flow-node ${step.status}${compact && finished ? " compact" : ""}`);
    const role = state.roles[step.role];
    if (role && step.status !== "pending") node.style.borderLeftColor = role.color;

    const title = el("div", "flow-title");
    const left = el("div", "row");
    left.append(el("span", "flow-index", `${index + 1}.`));
    const badge = el("span", "badge role", step.role);
    if (role) badge.style.background = role.color;
    left.append(badge);
    if (step.status === "running") left.append(el("span", "spin", "◐"));
    if (compact && finished) {
      left.append(el("span", "muted small", clip(step.task, 46)));
    }
    title.append(left, el("span", "badge", step.status));
    node.append(title);

    const task = el("div", "flow-task", step.task || "(no task)");
    node.append(task);

    const meta = el("div", "flow-meta");
    if (step.checker) {
      meta.append(el("div", "flow-chain",
        `↳ ${step.checker} · fail → ${step.fixer} fixes → ${step.role} again ` +
        `(${step.loop_limit} loop${step.loop_limit > 1 ? "s" : ""})`));
    }
    const attempts = step.attempts || [];
    if (attempts.length) {
      const last = attempts[attempts.length - 1];
      const gates = (last.gate_results || [])
        .map((g) => `${g.gate}:${g.verdict}`).join(" ");
      meta.append(el("div", "muted small",
        `attempt ${last.n}${gates ? ` · ${gates}` : ""}` +
        (last.files_written?.length ? ` · ${last.files_written.join(", ")}` : "")));
    }
    node.append(meta);

    // The whole card opens the detail view — a 400px column is no place to
    // read a prompt or a traceback.
    node.onclick = (e) => {
      if (e.target.tagName === "BUTTON") return;
      openStep(step, index);
    };
    box.append(node);
  });

  if (!steps.length) box.append(el("p", "muted small", "No steps yet."));
}

function openStep(step, index) {
  $("step-modal").classList.add("open");
  $("step-title").textContent = `${index + 1}. ${step.role} — ${step.status}`;
  const body = $("step-body");
  body.innerHTML = "";

  const head = el("div", "row small");
  const rerun = el("button", null, "rerun");
  rerun.onclick = async () => {
    const r = await api(`/api/sessions/${state.session.id}/steps/${step.id}/rerun`, { method: "POST" });
    toast(r.restarted ? `Re-running ${step.role}…`
                      : `${step.role} queued — the running engine will pick it up.`);
  };
  const skip = el("button", null, "skip");
  skip.onclick = () => api(`/api/sessions/${state.session.id}/steps/${step.id}/skip`, { method: "POST" });
  head.append(rerun, skip);
  if (step.checker) {
    head.append(el("span", "badge", `check: ${step.checker}`));
    head.append(el("span", "badge", `on fail: ${step.fixer}`));
  }
  body.append(head);

  body.append(rowWithCopy("task", step.task || ""));
  body.append(el("pre", "code", step.task || "(no task)"));

  // Everything this step's agents were actually sent, from the event stream.
  const stepEvents = state.events.filter((e) => e.step_id === step.id);
  const bundles = stepEvents.filter((e) => e.type === "context_bundle");
  const calls = stepEvents.filter((e) => e.type === "model_call");
  const tools = stepEvents.filter((e) => e.type === "tool_call");

  (step.attempts || []).forEach((a) => {
    const cls = a.verdict === "PASS" ? "pass" : a.verdict === "FAIL" ? "fail"
      : a.verdict ? "unknown" : "";
    const card = el("div", `attempt ${cls}`);
    card.append(el("div", "row small", ""));
    card.firstChild.append(el("span", "badge", `attempt ${a.n}`));
    (a.gate_results || []).forEach((g) => {
      const b = el("span", "badge", `${g.gate}: ${g.verdict}`);
      b.style.borderColor = g.verdict === "PASS" ? "var(--ok)"
        : g.verdict === "FAIL" ? "var(--err)" : "var(--warn)";
      card.firstChild.append(b);
    });
    if (!(a.gate_results || []).length) {
      card.firstChild.append(el("span", "badge", a.verdict || "no verdict"));
    }
    card.firstChild.append(
      el("span", "muted small", (a.files_written || []).join(", ") || "no files written"));
    if (a.feedback) {
      card.append(rowWithCopy("verifier feedback", a.feedback));
      card.append(el("pre", "code", a.feedback));
    }
    body.append(card);
  });

  if (step.summary) {
    body.append(rowWithCopy("result", step.summary));
    body.append(el("pre", "code", step.summary));
  }

  body.append(el("h3", null, `Context — ${calls.length} model call(s)`));
  if (bundles.length) {
    bundles.forEach((b) => {
      const d = el("details");
      d.append(el("summary", "muted small",
        `▸ curated bundle: ${b.payload.stats?.symbols ?? 0} symbols, ~${b.payload.stats?.est_tokens ?? 0} tok`));
      d.append(el("pre", "code", b.payload.rendered || ""));
      body.append(d);
    });
  }
  if (!calls.length) {
    body.append(el("p", "muted small", "No model calls recorded for this step in this view."));
  }
  calls.forEach((c) => body.append(renderEvent(c)));
  if (tools.length) {
    body.append(el("h3", null, `Tool calls — ${tools.length}`));
    tools.forEach((t) => body.append(renderEvent(t)));
  }
}

/* ─────────────── intercept a specific action while paused ─────────── */

function isPaused() {
  return !!(state.session && (state.session.paused || state.session.status === "paused"));
}

function paintPaused() {
  const hint = $("paused-hint");
  if (hint) hint.className = `paused-hint${isPaused() ? " on" : ""}`;
  document.querySelectorAll(".c-entry[data-ref]").forEach((entry) => {
    entry.classList.toggle("interceptable", isPaused());
  });
}

/** A one-line description of the action a console entry represents. */
function entryReference(event) {
  const p = event.payload || {};
  const d = p.detail || {};
  if (d.kind === "write") {
    return `your ${d.created ? "creation of" : "edit to"} ${d.path} (+${d.added} −${d.removed})`;
  }
  if (d.kind === "command") return `the command you ran: ${d.command} (exit ${d.exit_code})`;
  if (d.kind === "read") return `your read of ${d.path}`;
  if (event.type === "tool_call") return `your ${p.name} call`;
  if (event.type === "model_call") {
    const wants = (p.tool_calls || []).map((t) => t.name).join(", ");
    return wants ? `your decision to call ${wants}` : "your last message";
  }
  if (event.type === "verdict") return `the ${p.verdict} verdict`;
  return "that step";
}

function attachIntercept(entry, event) {
  entry.dataset.ref = "1";
  entry.classList.toggle("interceptable", isPaused());

  entry.querySelector(".c-head").addEventListener("click", (e) => {
    if (!isPaused()) return;                       // normal expand/collapse
    if (entry.querySelector(".intercept")) return; // already open
    e.stopPropagation();

    const box = el("div", "intercept");
    const reference = entryReference(event);
    box.append(el("div", "intercept-ref", `About ${reference}`));
    const note = el("textarea");
    note.placeholder = "e.g. don't hardcode the port here — read it from config";
    box.append(note);

    const row = el("div", "row");
    const cancel = el("button", null, "Cancel");
    const send = el("button", "primary", "Send + resume");
    const sendOnly = el("button", null, "Queue only");
    cancel.onclick = () => box.remove();

    const deliver = async (resume) => {
      const text = note.value.trim();
      if (!text) return toast("Write a note first.");
      const payload = `Regarding ${reference}: ${text}`;
      await api(`/api/sessions/${state.session.id}/steer`, {
        method: "POST", body: { note: payload, step_id: event.step_id || null },
      });
      box.remove();
      if (resume) {
        await api(`/api/sessions/${state.session.id}/resume`, { method: "POST" });
        toast("Sent — the agent will see it on its next turn.");
      } else {
        toast("Queued for the agent's next turn.");
      }
    };
    send.onclick = () => deliver(true);
    sendOnly.onclick = () => deliver(false);

    row.append(cancel, sendOnly, send);
    box.append(row);
    entry.append(box);
    entry.classList.add("c-open");
    note.focus();
  }, true);   // capture, so it runs before the expand/collapse handler
}
