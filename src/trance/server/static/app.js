"use strict";

const state = {
  session: null,
  roles: {},
  providers: [],
  presets: [],
  //: Step ids the user has explicitly opened while collapsing is on.
  openSteps: new Set(),
  commands: null,
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
  state.draftBase = draftFingerprint();   // what the server last gave us
  const box = $("flow-editor");
  box.innerHTML = "";
  if (!state.draftSteps.length) {
    box.append(el("p", "muted small", state.splitting
      ? "Waiting for the orchestrator to break the oversized steps up…"
      : "No steps yet — describe the project to the orchestrator, or add one manually."));
  }
  if (state.splitting) box.append(splittingNote());
  state.draftSteps.forEach((step, index) => box.append(stepCard(step, index)));
}

//: Only the parts a user edits — status and attempts change under them.
function draftFingerprint() {
  return JSON.stringify((state.draftSteps || []).map(
    (s) => [s.role, s.task, s.check, s.on_fail, s.max_loops]));
}

function splittingNote() {
  const note = el("div", "splitting-note");
  note.append(el("span", "spin", "◐"));
  note.append(el("span", null,
    `The orchestrator is breaking up ${state.splitting.count} step(s) over `
    + `${state.splitting.threshold} points. The plan below updates when it is done.`));
  return note;
}

//: A step you can no longer be waiting on — safe to fold away.
const FINISHED = new Set(["done", "failed", "skipped", "blocked"]);

/* A step nobody can hold in their head is where agents drift: the model does
 * the part it understood and reports success on the whole thing. The estimate
 * makes that visible before the run, and `split` is what you do about it. */

const POINT_LABEL = {
  1: "one small edit", 2: "one focused change", 3: "one file end to end",
  5: "a few files that must agree", 8: "a whole feature — consider splitting",
  13: "too big for one step",
};

function pointsBadge(step) {
  const limit = (state.planning && state.planning.max_step_points) || 0;
  const points = step.points || 0;
  const over = limit > 0 && points > limit;
  const badge = el("span", `badge points${over ? " over" : ""}`,
                   points ? `${points} pts` : "unrated");
  badge.title = points
    ? `${POINT_LABEL[points] || ""}${over ? ` — over the limit of ${limit}` : ""}`
    : "Not estimated. Split it to have the orchestrator size the pieces.";
  return badge;
}

async function splitStep(step, button) {
  if (!state.session) return toast("Save the flow first.");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "splitting…";
  try {
    const result = await api(
      `/api/sessions/${state.session.id}/steps/${step.id}/split`, { method: "POST" });
    if (!result.split) return toast(result.reason || "It was left as one step.");
    state.session.flow = result.flow;
    state.draftSteps = result.flow.steps.map((s) => ({ ...s }));
    redrawEditor();
    toast(`Split into ${result.into.length} steps.`);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

function stepCard(step, index) {
  const card = el("div", "step-card");
  // Only a step whose agent is mid-flight is locked; a failed or finished one
  // is a plan you may correct, and correcting it re-queues it.
  const editable = !["running", "verifying"].includes(step.status);
  const finished = FINISHED.has(step.status);
  const collapsed = finished && $("collapse-editor").checked
                    && !state.openSteps.has(step.id);
  if (collapsed) card.classList.add("collapsed");
  card.draggable = editable;
  card.dataset.index = index;
  const role = state.roles[step.role];
  if (role) card.style.borderLeftColor = role.color;

  const head = el("div", "row step-head");
  head.append(el("span", "flow-index", `${index + 1}.`));

  // Folded: a plain badge and the task. Open: the editable controls.
  if (collapsed) {
    const badge = el("span", "badge role", step.role);
    if (role) badge.style.background = role.color;
    head.append(badge, el("span", "badge", step.status));
    if (step.points) head.append(pointsBadge(step));
    head.append(el("span", "step-peek", clip(step.task, 70)));
    const open = el("button", "step-toggle", "▸");
    open.title = "Show this step";
    open.onclick = (e) => {
      e.stopPropagation();
      state.openSteps.add(step.id);
      redrawEditor();
    };
    head.append(open);
    // Clicking anywhere on the folded row opens it; the arrow is just a hint.
    head.onclick = () => {
      state.openSteps.add(step.id);
      redrawEditor();
    };
    card.append(head);
    card.addEventListener("dragstart", (e) => {
      card.classList.add("dragging");
      e.dataTransfer.setData("text/plain", String(index));
    });
    card.addEventListener("dragend", () => card.classList.remove("dragging"));
    card.addEventListener("dragover", (e) => e.preventDefault());
    card.addEventListener("drop", (e) => {
      e.preventDefault();
      const from = Number(e.dataTransfer.getData("text/plain"));
      if (Number.isNaN(from) || from === index) return;
      const [moved] = state.draftSteps.splice(from, 1);
      state.draftSteps.splice(index, 0, moved);
      redrawEditor();
    });
    return card;
  }

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

  head.append(pointsBadge(step));

  // Splitting is the fix for the number being too big, so it belongs next to it.
  const split = el("button", "small", "split");
  split.disabled = !editable || !state.session;
  split.title = "Ask the orchestrator to break this into smaller steps";
  split.onclick = () => splitStep(step, split);
  head.append(split);

  const remove = el("button", null, "✕");
  remove.disabled = !editable;
  remove.onclick = () => { state.draftSteps.splice(index, 1); redrawEditor(); };
  head.append(remove);

  if (finished) {
    const fold = el("button", "step-toggle", "▾");
    fold.title = "Fold this step away";
    fold.onclick = (e) => {
      e.stopPropagation();
      state.openSteps.delete(step.id);
      if (!$("collapse-editor").checked) $("collapse-editor").checked = true;
      redrawEditor();
    };
    head.append(fold);
  }

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
    check.title = "Independent check that the agent's report is true. It does not decide " +
                  "the loop — a false report stops the flow outright.";
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
    fixer.disabled = !editable;
    fixer.title = "Who tries to fix the problem when this step reports anything " +
                  "other than SUCCESS, before it runs again";
    fixer.onchange = () => { step.on_fail = fixer.value || null; drawGates(); };

    const loops = el("input", "compact tiny");
    loops.type = "number";
    loops.min = 1;
    loops.value = step.max_loops ?? 2;
    loops.disabled = !editable;
    loops.title = "How many times this step may run before the flow is halted";
    loops.onchange = () => { step.max_loops = Number(loops.value) || 1; drawGates(); };

    row.append(field("check", check), field("on fail", fixer), field("loops", loops));
    gatesBox.append(row);

    const who = step.on_fail || step.role;
    const limit = step.max_loops ?? 2;
    gatesBox.append(el("div", "loop-note",
      `${step.role} reports SUCCESS → next step. ` +
      `Anything else → ${who} fixes it → ${step.role} runs again ` +
      `(${limit} loop${limit > 1 ? "s" : ""}, then the flow halts).`));
    gatesBox.append(el("div", step.check ? "loop-note check-note" : "loop-note muted",
      step.check
        ? `${step.check} separately checks that the report is true. It never sends work `
          + `to ${who} — if ${step.role} claims SUCCESS and ${step.check} disagrees, `
          + `the flow stops.`
        : `No fact check — ${step.role}'s report of its own outcome is taken at face value.`));
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

$("collapse-editor").onchange = () => {
  state.openSteps.clear();      // the toggle is the master switch
  redrawEditor();
};

$("add-step").onclick = () => {
  state.draftSteps.push({ role: "backend", task: "", check: null, on_fail: null,
                          max_loops: 2, status: "pending" });
  redrawEditor();
  // A new step goes on the end, so put it in view rather than making the user
  // scroll past everything that is already done.
  const box = $("flow-editor");
  box.scrollTop = box.scrollHeight;
  const last = box.lastElementChild?.querySelector("textarea");
  if (last) last.focus();
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
  loadMemory();      // only the count; the notes load when the modal opens
}

/* ─────────────── a refusal, asked rather than enforced ─────────────── */

/* The agent's thread is blocked while this is on screen. A remit is a good
 * default and a bad absolute — but loosening it in advance defeats the point,
 * so the boundary asks instead. "always" writes the answer into the policy. */

function approvalEntry(event) {
  const p = event.payload || {};
  const wrap = el("div", "c-entry c-approval");
  wrap.id = `approval-${p.id}`;

  const head = el("div", "c-approval-head");
  head.append(el("span", "c-approval-icon", "🔒"));
  head.append(el("span", "c-approval-title", p.message || "Permission needed"));
  wrap.append(head);
  wrap.append(el("pre", "c-approval-subject", p.subject || ""));

  const row = el("div", "row small");
  const decide = async (decision, label) => {
    for (const b of row.querySelectorAll("button")) b.disabled = true;
    try {
      await api(`/api/sessions/${state.session.id}/approvals/${p.id}`,
                { method: "POST", body: { decision } });
      toast(label);
    } catch (_) {
      for (const b of row.querySelectorAll("button")) b.disabled = false;
    }
  };

  const once = el("button", "primary", "Allow once");
  once.onclick = () => decide("once", "Allowed for this action only.");
  const always = el("button", null, p.kind === "write"
    ? `Always allow ${p.agent} to write this`
    : `Always allow ${p.agent} to run this`);
  always.onclick = () => decide("always", "Allowed, and added to the policy.");
  const deny = el("button", "danger", "Deny");
  deny.onclick = () => decide("deny", "Refused — the agent was told why.");
  row.append(once, always, deny);
  wrap.append(row);

  if (p.timeout_s) {
    wrap.append(el("div", "muted small",
      `The agent is waiting. With no answer in ${Math.round(p.timeout_s / 60)} min `
      + "this is refused, which is what would have happened anyway."));
  }
  return wrap;
}

/* ───────────────── the team's shared memory ───────────────────────── */

/* Behind a button rather than on the run screen: it is a handful of lines that
 * change a few times a run, and it competes with the console for the space
 * that matters while an agent is working. The count on the button is what you
 * need at a glance; the notes themselves are a click away. */

async function loadMemory() {
  if (!state.session) return null;
  try {
    state.memory = await api(`/api/sessions/${state.session.id}/memory`);
  } catch (_) { return null; }
  paintMemoryCount();
  return state.memory;
}

function paintMemoryCount() {
  const badge = $("memory-count");
  if (!badge) return;
  const n = (state.memory && state.memory.notes.length) || 0;
  badge.textContent = n ? String(n) : "";
  badge.hidden = !n;
}

function renderMemory() {
  const box = $("memory-list");
  if (!box) return;
  const data = state.memory || { notes: [], raw: "", prompt_view: "" };
  $("memory-path").textContent = data.path || "";
  const cost = data.prompt_view
    ? `~${Math.round(data.prompt_view.length / 4)} tokens added to every agent's prompt`
    : "";
  $("memory-budget").textContent = data.oversized
    ? `${cost} — over ${data.max_notes} notes; it will be compacted after the next step`
    : cost;
  $("memory-budget").className = data.oversized ? "small c-exit-n" : "muted small";
  $("memory-compact").disabled = !data.notes.length;

  box.innerHTML = "";
  if (!data.notes.length) {
    box.append(el("p", "muted small",
      "Nothing yet. Agents write here when they decide something the others must "
      + "match — a route and its payload, a port, how to run the tests. You can also "
      + "write the first notes yourself with Edit."));
    return;
  }
  for (const line of data.notes) {
    // "- **backend**: the API is POST /api/games"
    const match = line.match(/^-\s*\*\*(.+?)\*\*:\s*(.*)$/);
    const row = el("div", "memory-note");
    const who = el("span", "badge role", match ? match[1] : "team");
    const role = state.roles[match ? match[1] : ""];
    if (role) who.style.background = role.color;
    row.append(who, el("span", null, match ? match[2] : line.replace(/^-\s*/, "")));
    box.append(row);
  }
}

function memoryEditing(on) {
  $("memory-editor").hidden = !on;
  $("memory-list").hidden = on;
  $("memory-edit").hidden = on;
  $("memory-save").hidden = !on;
  $("memory-cancel").hidden = !on;
}

async function openMemory() {
  await loadMemory();
  renderMemory();
  memoryEditing(false);
  $("memory").classList.add("open");
}

$("open-memory").onclick = openMemory;
$("close-memory").onclick = () => $("memory").classList.remove("open");

$("memory-edit").onclick = () => {
  $("memory-raw").value = (state.memory && state.memory.raw) || "";
  memoryEditing(true);
};

$("memory-cancel").onclick = () => memoryEditing(false);

$("memory-compact").onclick = async () => {
  const button = $("memory-compact");
  button.disabled = true;
  button.textContent = "compacting…";
  try {
    const result = await api(`/api/sessions/${state.session.id}/memory/compact`,
                             { method: "POST" });
    await loadMemory();
    renderMemory();
    toast(result.compacted
      ? `Compacted ${result.before} notes to ${result.after}. The originals are in ` +
        "memory.archive.md."
      : `Left unchanged — ${result.reason}.`);
  } finally {
    button.textContent = "Compact";
    button.disabled = false;
  }
};

$("memory-save").onclick = async () => {
  await api(`/api/sessions/${state.session.id}/memory`, {
    method: "PUT", body: { raw: $("memory-raw").value },
  });
  await loadMemory();
  renderMemory();
  memoryEditing(false);
  toast("Project memory updated — agents see this from the next step.");
};

$("btn-pause").onclick = async () => {
  await api(`/api/sessions/${state.session.id}/pause`, { method: "POST" });
  state.session.paused = true;
  paintPaused();
  toast("Paused — click a console line to send a note about that action.");
};
$("btn-resume").onclick = async () => {
  const r = await api(`/api/sessions/${state.session.id}/resume`, { method: "POST" });
  state.session.paused = false;
  paintPaused();
  if (!r.running) toast(`Nothing to resume — ${r.reason}.`);
  else if (r.restarted) toast("Restarted from the next pending step.");
};
$("btn-stop").onclick = () => api(`/api/sessions/${state.session.id}/stop`, { method: "POST" });
$("btn-back-plan").onclick = () => { renderFlowEditor(); show("plan"); };

/* ───────────────────────────── websocket ──────────────────────────── */

function isPlanning() {
  return $("screen-plan").classList.contains("active");
}

/* The plan arrives before splitting finishes, so a refined flow lands while the
 * user is already looking at it. Replacing their edits would be worse than a
 * stale plan, so an edited draft is left alone and told about instead. */
function applyRefinedFlow(payload) {
  state.splitting = null;
  if (!state.session) return;
  const edited = state.draftBase && draftFingerprint() !== state.draftBase;
  state.session.flow = payload.flow;
  if (!isPlanning()) { renderFlowView(); return; }
  if (edited) {
    toast("The orchestrator split some steps, but you have unsaved edits — "
          + "reload the flow to take them.");
    return;
  }
  renderFlowEditor();
  if (payload.message) toast(payload.message);
}

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
    if (event.type === "approval_requested") setActivity(
      event.payload.agent, "waiting for you to allow or refuse an action");
    if (event.type === "splitting_steps") {
      state.splitting = event.payload;
      if (isPlanning()) renderFlowEditor();
    }
    if (event.type === "flow_updated" && event.payload.flow) {
      applyRefinedFlow(event.payload);
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
    case "fixing":
      return `${p.message || "fixing"}` +
             (p.handoff_chars ? ` · handed ${p.handoff_chars} chars of context` : "");
    case "fixed": return `${clip(p.summary, 70)} · ${(p.files || []).join(", ") || "no files"}`;
    case "splitting_steps": return p.message;
    case "steps_split":
      return `${p.split.length} step(s) over ${p.threshold} points broken up — `
             + `${p.steps} steps now`;
    case "memory_compacted":
      return p.compacted ? `project memory: ${p.before} notes → ${p.after}`
                         : `memory left as it was — ${p.reason}`;
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
  state.planning = info.planning || { max_step_points: 0, scale: [1, 2, 3, 5, 8, 13] };
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
  renderStepSize();
  await renderProviders();
}

function renderStepSize() {
  const select = $("max-step-points");
  const planning = state.planning || { max_step_points: 0, scale: [1, 2, 3, 5, 8, 13] };
  select.innerHTML = "";
  const off = el("option", null, "never split");
  off.value = "0";
  select.append(off);
  for (const p of planning.scale) {
    const opt = el("option", null, `${p} points${POINT_LABEL[p] ? ` — ${POINT_LABEL[p]}` : ""}`);
    opt.value = String(p);
    select.append(opt);
  }
  select.value = String(planning.max_step_points || 0);

  const model = $("escalation-preset");
  model.innerHTML = "";
  const noEscalation = el("option", null, "none — halt instead");
  noEscalation.value = "";
  model.append(noEscalation);
  state.presets.forEach((m) => {
    const opt = el("option", null, `${m.name} — ${m.model}`);
    opt.value = m.name;
    model.append(opt);
  });
  model.value = planning.escalation_preset || "";

  const who = $("escalation-role");
  who.innerHTML = "";
  const same = el("option", null, "the step's own agent");
  same.value = "";
  who.append(same);
  Object.values(state.roles).filter((r) => r.name !== "orchestrator").forEach((r) => {
    const opt = el("option", null, r.name);
    opt.value = r.name;
    who.append(opt);
  });
  who.value = planning.escalation_role || "";

  const saveEscalation = async () => {
    const body = await api("/api/config/planning", {
      method: "PUT",
      body: { escalation_preset: model.value, escalation_role: who.value },
    });
    state.planning = { ...state.planning, ...body };
    toast(body.escalation_preset
      ? `Exhausted steps get one more try on ${body.escalation_preset}.`
      : "Exhausted steps will halt the run, as before.");
  };
  model.onchange = saveEscalation;
  who.onchange = saveEscalation;
  select.onchange = async () => {
    const body = await api("/api/config/planning",
                           { method: "PUT", body: { max_step_points: Number(select.value) } });
    state.planning = { ...state.planning, ...body };
    toast(body.max_step_points
      ? `Steps above ${body.max_step_points} points will be split.`
      : "Steps will be estimated but never split.");
    if (state.draftSteps) redrawEditor();
  };
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

  // The connection lives on the model now: one definition, not two.
  const kind = el("select", "compact");
  Object.entries(state.kinds || {}).forEach(([id, spec]) => {
    const opt = el("option", null, `${spec.label || id}`);
    opt.value = id;
    if (id === preset.kind) opt.selected = true;
    kind.append(opt);
  });
  const borrowed = el("option", null, "— use a shared provider —");
  borrowed.value = "";
  if (!preset.kind) borrowed.selected = true;
  kind.prepend(borrowed);

  const provider = el("select", "compact");
  state.providers.forEach((p) => {
    const opt = el("option", null, `${p.name} (${p.kind})`);
    opt.value = p.name;
    if (p.name === preset.provider) opt.selected = true;
    provider.append(opt);
  });

  const baseUrl = el("input", "compact");
  baseUrl.value = preset.base_url || "";
  baseUrl.placeholder = "default for this API";
  baseUrl.title = "Endpoint. Blank uses the default for the API kind.";

  const key = el("input", "compact");
  key.type = "password";
  key.value = preset.has_key ? "***" : "";
  key.placeholder = preset.has_key ? "saved — leave to keep" : "none needed locally";
  key.title = "Sent as the API key. Leave the dots alone to keep the stored one.";

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
  ctx.placeholder = "default for this API";
  ctx.title = "How much this model can hold. Set it when it differs from the API default " +
              "(Haiku is 200k where Opus is 1M), and when your llama-server was started " +
              "with a smaller -c. Wrong values 400 mid-run.";

  const out = el("input", "compact");
  out.type = "number";
  out.value = preset.max_tokens || "";
  out.placeholder = "auto (⅛ of window)";
  out.title = "Longest single reply this model may produce. A tool call that runs " +
              "past it is cut off mid-argument and rejected, so a model that writes " +
              "whole files needs room. It is taken out of the context window.";

  const providerField = wrap("Shared provider", provider);
  const urlField = wrap("Base URL", baseUrl);
  const keyField = wrap("API key", key);

  // Either the model brings its own endpoint or it borrows one — showing both
  // at once is the two-definitions problem all over again.
  const showConnection = () => {
    const own = !!kind.value;
    providerField.hidden = own;
    urlField.hidden = !own;
    keyField.hidden = !own;
  };
  kind.onchange = showConnection;

  grid.append(wrap("Name (agents pick this)", name), wrap("API", kind),
              providerField, urlField, keyField,
              wrap("Model id", model), wrap("Context window", ctx),
              wrap("Max output", out));
  showConnection();

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
        kind: kind.value,
        provider: kind.value ? "" : provider.value,
        base_url: kind.value ? baseUrl.value.trim() : "",
        // "***" means "keep what is stored"; the server drops it.
        ...(kind.value && key.value !== "***" ? { api_key: key.value.trim() } : {}),
        model: model.value.trim(),
        context_window: Number(ctx.value) || 0,
        max_tokens: Number(out.value) || 0,
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
  // A new model brings its own endpoint. Nothing else has to exist first.
  const kind = Object.keys(state.kinds || {})[0] || "llamacpp";
  $("preset-list").append(presetCard(
    { name: "", kind, base_url: "", model: "", provider: "" }, true));
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
  // The card offers a list picker, so the names have to be here first.
  if (!state.commands) {
    try { state.commands = await api("/api/commands"); } catch (_) { /* offline */ }
  }
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
  // Folded by default: it is the longest part of the card and the least often
  // edited, and an unfolded one pushed every other agent off the screen.
  const permsWrap = el("details", "perm-fold");
  const permsSummary = el("summary", "muted small");
  permsSummary.append(el("span", null, "permissions"));
  permsSummary.append(el("span", "perm-peek",
    `${(agent.toolsets || []).join(", ") || "no tools"} · `
    + `${(agent.paths || []).length} path rule(s)`
    + (agent.verifier ? " · verifier" : "")));
  permsWrap.append(permsSummary);
  const perms = el("div", "perm-box");
  permsWrap.append(perms);
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
  const listSel = el("select", "compact");
  const listNames = (state.commands && state.commands.names) || ["default"];
  listNames.forEach((n) => {
    const opt = el("option", null, n);
    opt.value = n === "default" ? "" : n;
    if ((agent.command_list || "") === opt.value) opt.selected = true;
    listSel.append(opt);
  });
  listSel.title = "Which named allowlist this agent uses. Edit the lists in the $_ modal.";

  const commands = el("input", "compact");
  commands.value = (agent.commands || []).join(" ");
  commands.placeholder = "use the list";
  commands.title = "Space-separated programs, replacing the named list entirely for "
                   + "this agent. Blank is the normal case.";
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
  const shellSel = el("select", "compact");
  [["", "follow global"], ["yes", "shell on"], ["no", "shell off"]].forEach(([v, label]) => {
    const opt = el("option", null, label);
    opt.value = v;
    if (v === (agent.shell === null || agent.shell === undefined ? "" : agent.shell ? "yes" : "no")) {
      opt.selected = true;
    }
    shellSel.append(opt);
  });
  shellSel.title = "Pipes, redirects and && for this agent";
  cmdRow.append(wrapField("Command list", listSel, "shared, edited in $_"),
                wrapField("Own commands", commands, "blank = use the list"),
                wrapField("Pipes / redirects", shellSel),
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
      command_list: listSel.value,
      verifier: verifierBox.checked,
      commands: commands.value.split(/[\s,]+/).filter(Boolean),
      shell: shellSel.value === "" ? null : shellSel.value === "yes",
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

  card.append(head, grid, permsWrap, promptWrap, actions);
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
        `↳ fact-checked by ${step.checker} · a false report halts the run`));
    }
    meta.append(el("div", "flow-chain",
      `↻ not success → ${step.fixer} fixes → ${step.role} again ` +
      `(${step.loop_limit} loop${step.loop_limit > 1 ? "s" : ""})`));
    const attempts = step.attempts || [];
    if (attempts.length) {
      const last = attempts[attempts.length - 1];
      const parts = [`attempt ${last.n}`];
      if (last.outcome) parts.push(`outcome ${last.outcome}`);
      (last.gate_results || []).forEach((g) => parts.push(`${g.gate}:${g.verdict}`));
      if (last.files_written?.length) parts.push(last.files_written.join(", "));
      meta.append(el("div", "muted small", parts.join(" · ")));
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
    // A stop only lands when the model call returns, so "nothing happened" is
    // usually "waiting" — say which, or the button reads as broken.
    const resumed = r.resumed ? " Un-paused." : "";
    toast(r.restarted ? `Re-running ${step.role}…${resumed}`
          : r.waiting_for ? `${step.role} queued — starts when ${r.waiting_for}.${resumed}`
          : `${step.role} queued — the running engine will pick it up.${resumed}`);
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
    if (a.outcome) {
      const b = el("span", "badge", `outcome: ${a.outcome}`);
      b.style.borderColor = a.outcome === "SUCCESS" ? "var(--ok)" : "var(--err)";
      card.firstChild.append(b);
    }
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
    if (a.outcome_reason) {
      card.append(rowWithCopy("why the step failed", a.outcome_reason));
      card.append(el("pre", "code", a.outcome_reason));
    }
    if (a.feedback) {
      card.append(rowWithCopy("fact check", a.feedback));
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

/* ────────────────────────── allowed commands ──────────────────────── */

$("open-commands").onclick = openCommands;
$("close-commands").onclick = () => $("commands").classList.remove("open");
$("commands").onclick = (e) => { if (e.target.id === "commands") e.currentTarget.classList.remove("open"); };

async function openCommands() {
  $("commands").classList.add("open");
  await renderCommands();
}

async function renderCommands(select) {
  const data = await api("/api/commands");
  state.commands = data;
  const names = data.names || [data.default];
  const current = names.includes(select) ? select
    : names.includes(state.commandList) ? state.commandList : data.default;
  state.commandList = current;

  const picker = $("cmd-which");
  picker.innerHTML = "";
  names.forEach((name) => {
    const opt = el("option", null, name === data.default ? `${name} (default)` : name);
    opt.value = name;
    if (name === current) opt.selected = true;
    picker.append(opt);
  });
  picker.onchange = () => renderCommands(picker.value);
  $("cmd-delete").disabled = current === data.default;

  const users = Object.entries(data.usage || {})
    .filter(([, list]) => list === current).map(([agent]) => agent);
  $("cmd-used-by").textContent = users.length
    ? `used by ${users.join(", ")}`
    : "no agent uses this list yet";

  const policy = (data.lists || {})[current] || data;
  $("cmd-shell").checked = !!policy.shell;
  $("cmd-list").value = (policy.allowed || []).join("\n");

  const box = $("cmd-overrides");
  box.innerHTML = "";
  const overridden = Object.keys(data.overrides || {});
  if (!overridden.length) {
    box.append(el("p", "muted small",
      `No overrides — ${(data.agents_with_commands || []).join(", ") || "no agents"} use a named list.`));
    return;
  }
  overridden.forEach((name) => {
    const o = data.overrides[name];
    const card = el("div", "provider-card");
    const head = el("div", "row");
    const badge = el("span", "badge role", name);
    const role = state.roles[name];
    if (role) badge.style.background = role.color;
    head.append(badge);
    if (o.workdir) head.append(el("span", "badge", `runs in ${o.workdir}`));
    if (o.shell !== null && o.shell !== undefined) {
      head.append(el("span", "badge", o.shell ? "shell on" : "shell off"));
    }
    card.append(head);
    card.append(el("code", null,
      (o.commands || []).join("  ") || `(uses the ${data.usage[name] || data.default} list)`));
    box.append(card);
  });
}

$("cmd-save").onclick = async () => {
  const allowed = $("cmd-list").value.split(/[\s,]+/).filter(Boolean);
  if (!allowed.length) return toast("The allowlist cannot be empty.");
  try {
    await api("/api/commands", {
      method: "PUT",
      body: { name: state.commandList, allowed, shell: $("cmd-shell").checked },
    });
  } catch (_) { return; }
  $("cmd-result").textContent = `saved — ${allowed.length} programs in ${state.commandList}`;
  $("cmd-result").className = "check-result ok";
  toast(`“${state.commandList}” updated.`);
  await renderCommands(state.commandList);
};

$("cmd-new").onclick = async () => {
  const name = (prompt("Name for the new list (no spaces), e.g. build-tools") || "").trim();
  if (!name) return;
  if (name.includes(" ")) return toast("A list name cannot contain spaces.");
  try {
    // Starts from the built-in defaults rather than empty: a list with no
    // programs refuses everything, which never reads as intentional.
    await api("/api/commands", {
      method: "PUT", body: { name, allowed: state.commands.defaults, shell: true },
    });
  } catch (_) { return; }
  await renderCommands(name);
  toast(`Created “${name}”. Point an agent at it in 👥 Agents.`);
};

$("cmd-delete").onclick = async () => {
  const name = state.commandList;
  const ok = await confirmDialog(`Delete the “${name}” list?`,
    "Agents using it fall back to the default list.");
  if (!ok) return;
  const result = await api(`/api/commands/${encodeURIComponent(name)}`, { method: "DELETE" });
  await renderCommands(state.commands.default);
  toast(result.moved_to_default.length
    ? `Deleted. ${result.moved_to_default.join(", ")} now use the default list.`
    : "Deleted.");
};

$("cmd-reset").onclick = async () => {
  const ok = await confirmDialog("Reset the allowlist to defaults?",
    "Per-agent overrides are not affected.");
  if (!ok) return;
  await api("/api/commands/reset", { method: "POST", body: { name: state.commandList } });
  await renderCommands(state.commandList);
  toast("Reset to defaults.");
};

/* ─────────────────── who is working, right now ────────────────────── */

const activity = { agent: null, what: "", since: 0, model: "", context: null };
let activityTimer = null;

/* How full the running agent's window is. The whole point of trance is keeping
 * this number small, so it belongs on screen while the agent works — not
 * buried in an event you have to expand. */

function fmtTokens(n) {
  if (!Number.isFinite(n)) return "?";
  if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1).replace(/\.0$/, "") + "k";
  return String(n);
}

//: Pressure bands. Above `budget` the runner starts dropping tool results, so
//: that is the line that actually costs the agent something, not 100%.
function contextLevel(ctx) {
  const budget = ctx.budget || ctx.window;
  if (ctx.trimmed || ctx.tokens >= budget) return "hot";
  if (ctx.tokens >= budget * 0.75) return "warm";
  return "cool";
}

function svg(tag, attrs) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, String(v));
  return node;
}

function contextGauge(ctx) {
  const pct = Math.max(0, Math.min(100, ctx.percent ?? 0));
  const level = contextLevel(ctx);
  const wrap = el("span", `ctx-gauge ${level}`);

  const R = 8, C = 2 * Math.PI * R;
  const ring = svg("svg", { class: "ctx-ring", viewBox: "0 0 20 20", width: 20, height: 20 });
  ring.append(svg("circle", { class: "ctx-track", cx: 10, cy: 10, r: R }));
  ring.append(svg("circle", {
    class: "ctx-fill", cx: 10, cy: 10, r: R,
    "stroke-dasharray": `${(C * pct) / 100} ${C}`,
    transform: "rotate(-90 10 10)",
  }));
  // Where trimming begins, so the ring shows the limit that actually bites.
  if (ctx.budget && ctx.window) {
    const mark = (100 * ctx.budget) / ctx.window;
    ring.append(svg("circle", {
      class: "ctx-mark", cx: 10, cy: 10, r: R,
      "stroke-dasharray": `1 ${C}`,
      "stroke-dashoffset": -(C * mark) / 100,
      transform: "rotate(-90 10 10)",
    }));
  }

  wrap.append(ring);
  wrap.append(el("b", "ctx-pct", `${pct < 10 ? pct.toFixed(1) : Math.round(pct)}%`));
  wrap.append(el("span", "ctx-abs", `${fmtTokens(ctx.tokens)} / ${fmtTokens(ctx.window)}`));
  if (ctx.trimmed) wrap.append(el("span", "ctx-flag", "trimmed"));

  wrap.title = [
    `${ctx.tokens.toLocaleString()} of ${(ctx.window || 0).toLocaleString()} tokens` +
      (ctx.estimated ? " (estimated — the endpoint reported no usage)" : ""),
    `Trimming starts at ${(ctx.budget || 0).toLocaleString()} ` +
      `(${(ctx.reserved || 0).toLocaleString()} reserved for the reply).`,
    ctx.trimmed ? `${ctx.trimmed} tool result(s) dropped to fit.` : "",
  ].filter(Boolean).join("\n");
  return wrap;
}

function setActivity(agent, what, model) {
  if (agent !== activity.agent || what !== activity.what) {
    if (agent !== activity.agent) activity.since = Date.now();
    activity.agent = agent;
    activity.what = what;
    if (model) activity.model = model;
  }
  if (!activityTimer) activityTimer = setInterval(paintActivity, 1000);
  paintActivity();
}

function setContext(ctx) {
  if (!ctx || !ctx.window) return;
  // A trim happens on the round *before* the call it protected, so carry the
  // flag forward until the next call reports its own numbers.
  activity.context = { ...ctx, trimmed: 0 };
  paintActivity();
}

function noteTrim(dropped, window) {
  const prev = activity.context || { tokens: 0, window, budget: 0, percent: 0 };
  activity.context = { ...prev, trimmed: (prev.trimmed || 0) + (dropped || 0) };
  paintActivity();
}

function clearActivity() {
  activity.agent = null;
  activity.what = "";
  activity.context = null;
  clearInterval(activityTimer);
  activityTimer = null;
  paintActivity();
}

function elapsed(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

function paintActivity() {
  const header = $("now-working");
  const strip = $("activity-strip");
  if (!activity.agent) {
    header.className = "now-working";
    header.innerHTML = "";
    if (strip) { strip.className = "activity-strip"; strip.innerHTML = ""; }
    return;
  }
  const role = state.roles[activity.agent];
  const since = elapsed(Date.now() - activity.since);

  header.className = "now-working active";
  header.innerHTML = "";
  header.append(el("span", "dot"));
  const badge = el("span", "badge role", activity.agent);
  if (role) badge.style.background = role.color;
  header.append(badge, el("span", "muted", clip(activity.what, 40)), el("span", "activity-elapsed", since));

  if (strip) {
    strip.className = "activity-strip active";
    strip.innerHTML = "";
    const b2 = el("span", "badge role", activity.agent);
    if (role) b2.style.background = role.color;
    strip.append(el("span", "dot"), b2,
                 el("span", "activity-what", activity.what),
                 el("span", "activity-elapsed", since));
    if (activity.model) strip.append(el("span", "muted small", shortModel(activity.model)));
    if (activity.context) strip.append(contextGauge(activity.context));
  }
}

function trackActivity(event) {
  const p = event.payload || {};
  switch (event.type) {
    case "step_started":
      // Each step starts a fresh conversation, so the old fill is not this
      // agent's and would read as context it is still carrying.
      activity.context = null;
      setActivity(event.agent, clip(p.task, 70) || "working"); break;
    case "step_verifying":
      setActivity(p.verifier || event.agent, "verifying the previous step"); break;
    case "model_call":
      setActivity(event.agent, p.tool_calls?.length
        ? `deciding — ${p.tool_calls.map((t) => t.name).join(", ")}`
        : "thinking", p.model);
      setContext(p.context); break;
    case "context_trimmed":
      noteTrim(p.dropped_tool_results, p.context_window); break;
    case "tool_call":
      setActivity(event.agent, `${p.name}(${Object.values(p.arguments || {})
        .map((v) => clip(String(v), 20)).join(", ")})`); break;
    case "context_bundle":
      setActivity(event.agent, "receiving curated context"); break;
    case "tool_call":
      // Keep the button's count honest while agents are writing notes.
      if ((p.detail || {}).kind === "memory" && p.detail.stored) loadMemory();
      break;
    case "fixing":
      activity.context = null;      // the fixer starts its own conversation
      setActivity(event.agent, "fixing what the last pass reported"); break;
    case "index":
      setActivity("orchestrator", "indexing the project"); break;
    case "step_finished": case "step_failed": case "run_finished":
    case "run_stopped": case "paused": case "error":
      clearActivity(); break;
    default: break;
  }
}

/* ═══════════════════ live console of the working agent ═════════════ */

const ICON = {
  write: "✎", create: "✚", cmd: "$", read: "◇", graph: "⌕",
  think: "◐", tool: "▸", fail: "✕", step: "▶", verdict: "✓",
};
let consoleStep = null;

function consoleReset(label) {
  const box = $("console");
  if (!box) return;
  box.innerHTML = "";
  box.append(el("div", "c-empty", label || "Waiting for the agent to do something…"));
  const scope = $("console-scope");
  if (scope) scope.textContent = "";
}

function atBottom(box) {
  return box.scrollHeight - box.scrollTop - box.clientHeight < 80;
}

//: The event consoleAppend() is currently rendering, so consolePush can bind
//: the intercept affordance without every call site having to pass it.
let renderingEvent = null;

function consolePush(node) {
  if (renderingEvent) attachIntercept(node, renderingEvent);
  const box = $("console");
  if (!box) return;
  const empty = box.querySelector(".c-empty");
  if (empty) empty.remove();
  const stick = $("follow").checked || atBottom(box);
  box.append(node);
  if (stick) box.scrollTop = box.scrollHeight;
}

/** One collapsible console line. `body` builds the expanded content lazily. */
function consoleEntry({ kind, icon, label, tag, time, body, open = false, failed = false }) {
  const entry = el("div", `c-entry c-${kind}${failed ? " c-fail" : ""}`);
  const head = el("div", "c-head");
  head.append(el("span", "c-time", time));
  head.append(el("span", "c-icon", icon));
  const text = el("span", "c-label");
  if (typeof label === "string") text.textContent = label; else text.append(label);
  head.append(text);
  if (tag) head.append(el("span", "c-tag", tag));
  entry.append(head);

  if (body) {
    const wrap = el("div", "c-body");
    wrap.style.display = open ? "" : "none";
    if (open) { wrap.append(body()); entry.classList.add("c-open"); }
    let built = open;
    head.onclick = () => {
      const showing = wrap.style.display !== "none";
      if (!built && !showing) { wrap.append(body()); built = true; }
      wrap.style.display = showing ? "none" : "";
      entry.classList.toggle("c-open", !showing);
    };
    entry.append(wrap);
  }
  return entry;
}

function renderDiff(diff) {
  const pre = el("pre", "diff");
  (diff || "").split("\n").forEach((line) => {
    let cls = "l";
    if (line.startsWith("+++") || line.startsWith("---")) cls = "l l-meta";
    else if (line.startsWith("@@")) cls = "l l-hunk";
    else if (line.startsWith("+")) cls = "l l-add";
    else if (line.startsWith("-")) cls = "l l-del";
    pre.append(el("span", cls, line || " "));
  });
  return pre;
}

function labelWith(parts) {
  const span = el("span");
  parts.forEach(([text, cls]) => span.append(el("span", cls || null, text)));
  return span;
}

function consoleAppend(event) {
  const box = $("console");
  if (!box) return;
  renderingEvent = event;
  const p = event.payload || {};
  const time = new Date(event.ts).toLocaleTimeString();
  const showReads = $("show-reads").checked;

  // Scope the console to the step being worked on — the full history for a
  // finished step stays available by clicking it in the pipeline.
  if (event.type === "step_started") {
    consoleStep = event.step_id;
    box.innerHTML = "";
    $("console-scope").textContent = `${event.agent} · attempt ${p.attempt || 1}`;
    consolePush(consoleEntry({
      kind: "step", icon: ICON.step, time, tag: event.agent,
      label: labelWith([[clip(p.task, 90), ""]]),
    }));
    return;
  }
  if (event.step_id && consoleStep && event.step_id !== consoleStep) return;

  switch (event.type) {
    case "tool_call": {
      const d = p.detail || {};
      // `ok === false` covers both "never ran" and "ran and exited non-zero".
      // Only the first is a refusal; a failing command has a detail and is
      // rendered below with its exit code.
      const refused = p.ok === false && !d.kind;

      if (refused) {
        const head = labelWith([[`${p.name} refused`, ""]]);
        if (d.kind === "refused_program") head.append(allowButton(d));
        consolePush(consoleEntry({
          kind: "read", icon: ICON.fail, time, failed: true, tag: event.agent,
          label: head,
          body: () => el("pre", null, p.result || ""),
          open: true,
        }));
        return;
      }
      if (d.kind === "write") {
        consolePush(consoleEntry({
          kind: "write", icon: d.created ? ICON.create : ICON.write, time, tag: event.agent,
          label: labelWith([
            [d.appended ? "append " : d.created ? "create " : "edit ", ""], [d.path, "c-path"],
            ["  +" + d.added, "c-add"], [" −" + d.removed, "c-del"],
          ]),
          body: () => {
            const wrap = el("div");
            wrap.append(copyButton(() => d.diff || "", "copy diff"));
            wrap.append(renderDiff(d.diff));
            if (d.truncated) wrap.append(el("div", "muted small", "diff truncated for display"));
            return wrap;
          },
          open: true,
        }));
        return;
      }
      if (d.kind === "background") {
        consolePush(consoleEntry({
          kind: "cmd", icon: "⇥", time, tag: event.agent,
          label: labelWith([[d.command, ""], ["  running in background", "c-exit-0"]]),
          body: () => el("pre", null, p.result || ""),
        }));
        return;
      }
      if (d.kind === "graph") {
        // A lookup that found nothing is not a refusal — the tool answered.
        // Misses show even with reads hidden: they are how you notice an agent
        // searching for phrases the graph could never contain.
        if (d.hit && !showReads) return;
        consolePush(consoleEntry({
          kind: "graph", icon: ICON.graph, time, tag: event.agent,
          label: labelWith([
            [`${p.name} `, ""], [clip(d.query, 48), "c-path"],
            [d.hit ? "" : "  no match", "muted"],
          ]),
          body: () => el("pre", null, p.result || ""),
          open: !d.hit,
        }));
        return;
      }
      if (d.kind === "memory") {
        consolePush(consoleEntry({
          kind: "write", icon: "🧠", time, tag: event.agent,
          label: labelWith([
            [d.stored ? "remembered " : "already known ", ""],
            [clip(d.note, 80), "c-path"],
          ]),
          body: () => el("pre", null, p.result || ""),
        }));
        return;
      }
      if (d.kind === "truncated") {
        // Not a refusal: the model simply ran past its output limit mid-call,
        // so nothing ran. Say which limit, since raising it is the real fix.
        consolePush(consoleEntry({
          kind: "read", icon: ICON.fail, time, failed: true, tag: event.agent,
          label: labelWith([
            ["tool call cut off", ""],
            [`  ${d.limit}-token output limit`, "c-exit-n"],
          ]),
          body: () => el("pre", null, p.result || ""),
          open: true,
        }));
        return;
      }
      if (d.kind === "command_stopped") {
        consolePush(consoleEntry({
          kind: "read", icon: "⏹", time, tag: event.agent,
          label: `stopped ${d.command_id}`,
        }));
        return;
      }
      if (d.kind === "command") {
        const failedCmd = d.exit_code !== 0 || d.timed_out || d.cancelled;
        consolePush(consoleEntry({
          kind: "cmd", icon: ICON.cmd, time, tag: event.agent, failed: failedCmd,
          label: labelWith([
            [d.command, ""],
            [d.timed_out ? `  timed out after ${d.seconds}s`
              : d.cancelled ? "  cancelled"
              : `  exit ${d.exit_code}${d.seconds > 3 ? ` · ${d.seconds}s` : ""}`,
             failedCmd ? "c-exit-n" : "c-exit-0"],
          ]),
          body: () => {
            const wrap = el("div");
            wrap.append(copyButton(() => `$ ${d.command}\n${d.output}`, "copy"));
            wrap.append(el("pre", null, d.output || "(no output)"));
            return wrap;
          },
          open: failedCmd,
        }));
        return;
      }
      if (d.kind === "read" || ["get_definition", "get_callers", "get_callees", "search_symbols",
                               "list_files"].includes(p.name)) {
        if (!showReads) return;
        const what = d.path || Object.values(p.arguments || {})[0] || "";
        if (d.deduped) {
          // Worth seeing: a run full of these means the agent is going in
          // circles, even though the context cost is now near zero.
          consolePush(consoleEntry({
            kind: "read", icon: "⟲", time, tag: event.agent,
            label: labelWith([[`${p.name} `, ""], [String(what), "c-path"],
                              ["  already in context", "muted"]]),
          }));
          return;
        }
        consolePush(consoleEntry({
          kind: d.kind === "read" ? "read" : "graph",
          icon: d.kind === "read" ? ICON.read : ICON.graph, time, tag: event.agent,
          label: labelWith([
            [`${p.name} `, ""], [String(what), "c-path"],
            [d.outline ? `  outline · ${d.symbols} symbols`
             : d.lines && d.last_line && d.last_line < d.lines
               ? `  lines ${d.start_line}-${d.last_line} of ${d.lines}` : "", "muted"],
          ]),
          body: () => el("pre", null, p.result || ""),
        }));
        return;
      }
      consolePush(consoleEntry({
        kind: "read", icon: ICON.tool, time, tag: event.agent,
        label: `${p.name}(${Object.values(p.arguments || {}).map((v) => clip(String(v), 24)).join(", ")})`,
        body: () => el("pre", null, p.result || ""),
      }));
      return;
    }

    case "model_call": {
      if (p.asked_for_outcome) {
        consolePush(consoleEntry({
          kind: "think", icon: "?", time, tag: shortModel(p.model),
          label: `asked for an outcome → ${clip(p.response_text, 60)}`,
          body: () => el("pre", null, p.response_text || ""),
        }));
        return;
      }
      const wants = (p.tool_calls || []).map((t) => t.name);
      const label = wants.length
        ? `thinking → ${wants.join(", ")}`
        : (clip(p.response_text, 80) || "thinking");
      consolePush(consoleEntry({
        kind: "think", icon: ICON.think, time, tag: shortModel(p.model),
        label,
        body: () => {
          const wrap = el("div");
          if (p.reasoning) {
            wrap.append(el("div", "msg-role", "reasoning"));
            wrap.append(el("pre", null, p.reasoning));
          }
          if (p.response_text) {
            wrap.append(copyButton(() => p.response_text, "copy"));
            wrap.append(el("pre", null, p.response_text));
          }
          if (!p.reasoning && !p.response_text) wrap.append(el("pre", null, "(tool call only)"));
          return wrap;
        },
      }));
      return;
    }

    case "command_started":
      pushRunningCommand(event);
      return;

    case "command_finished":
      finishRunningCommand(event);
      return;

    case "background_stopped":
      consolePush(consoleEntry({
        kind: "read", icon: "⏹", time, tag: event.agent, label: p.message,
      }));
      return;

    case "step_outcome": {
      const bad = p.outcome !== "SUCCESS";
      const label = p.outcome === "SUCCESS" ? "outcome: SUCCESS"
        : p.outcome === "UNSTATED" ? "outcome: not stated — treated as unfinished"
        : p.outcome === "UNCLEAR"
          ? `outcome unreadable — said neither SUCCESS nor FAILED: ${clip(p.reason, 60)}`
        : `outcome: FAILED — ${clip(p.reason, 80)}`;
      consolePush(consoleEntry({
        kind: bad ? "cmd" : "write", icon: bad ? "!" : ICON.verdict, time,
        tag: event.agent, failed: bad, label, open: bad,
        body: bad ? () => el("pre", null, p.reason || "") : null,
      }));
      return;
    }

    case "run_halted":
      consolePush(consoleEntry({
        kind: "cmd", icon: "⛔", time, tag: event.agent, failed: true,
        label: p.message, open: true,
        body: () => el("pre", null, p.hint || ""),
      }));
      return;

    case "gate_failed":
      consolePush(consoleEntry({
        kind: "cmd", icon: "↺", time, tag: event.agent, failed: true,
        label: p.message, open: false,
      }));
      return;

    case "verdict":
      consolePush(consoleEntry({
        kind: p.verdict === "PASS" ? "write" : "cmd", icon: ICON.verdict, time,
        tag: event.agent, failed: p.verdict === "FAIL",
        label: `${p.gate || event.agent} (${p.position}/${p.of}): ${p.verdict}`,
        body: () => el("pre", null, p.detail || ""), open: p.verdict !== "PASS",
      }));
      return;

    case "step_verifying":
      $("console-scope").textContent =
        `${p.verifier} · check ${p.gate || 1}/${p.of || 1}`;
      consolePush(consoleEntry({
        kind: "step", icon: ICON.step, time, tag: p.verifier,
        label: `check ${p.gate || 1} of ${p.of || 1}` +
               (p.chain ? ` — ${p.chain.join(" → ")}` : ""),
      }));
      return;

    case "step_retry":
      consolePush(consoleEntry({
        kind: "step", icon: "↺", time, tag: event.agent, label: p.message || "retrying",
        body: () => el("pre", null, p.feedback || ""), open: false,
      }));
      return;

    case "fixing":
      // The handoff is the fixer's whole picture of what went wrong, so make it
      // readable here rather than only in the raw event.
      consolePush(consoleEntry({
        kind: "step", icon: "🛠", time, tag: event.agent,
        label: labelWith([
          [p.message || "fixing", ""],
          [p.handoff_chars ? `  handed ${Math.round(p.handoff_chars / 4)} tok of context` : "",
           "c-exit-0"],
        ]),
        body: () => {
          const wrap = el("div");
          if (p.handoff) {
            wrap.append(copyButton(() => p.handoff, "copy handoff"));
            wrap.append(el("div", "msg-role", `what ${event.agent} was handed`));
          }
          wrap.append(el("pre", null, p.handoff || "(nothing to hand over)"));
          return wrap;
        },
      }));
      return;

    case "approval_requested":
      consolePush(approvalEntry(event));
      return;

    case "approval_resolved": {
      // Replace the live card with what was decided, so scrolling back does
      // not show a question that is no longer open.
      const card = document.getElementById(`approval-${p.id}`);
      if (card) {
        card.className = "c-approval settled";
        card.innerHTML = "";
        const said = p.decision === "deny"
          ? (p.detail && p.detail.timed_out ? "no answer in time — refused"
             : p.detail && p.detail.abandoned ? "run stopped — refused" : "refused")
          : p.decision === "always" ? "allowed, and allowed from now on" : "allowed once";
        card.append(el("span", "c-approval-said", `${p.subject} — ${said}`));
      }
      return;
    }

    case "escalated":
      consolePush(consoleEntry({
        kind: "step", icon: "⇧", time, tag: event.agent,
        label: labelWith([[p.message || "escalating", ""],
                          [`  ${p.role} on ${shortModel(p.model)}`, "c-path"]]),
        open: true,
      }));
      return;

    case "escalation_failed":
      consolePush(consoleEntry({
        kind: "cmd", icon: ICON.fail, time, tag: event.agent, failed: true,
        label: `the stronger model did not fix it either — ${clip(p.reason, 70)}`,
        body: () => el("pre", null, p.reason || ""),
      }));
      return;

    case "splitting_steps":
      consolePush(consoleEntry({
        kind: "step", icon: "✂", time, tag: "orchestrator",
        label: p.message || "breaking up oversized steps",
        body: () => el("pre", null, (p.tasks || []).join("\n")),
      }));
      return;

    case "steps_split":
      consolePush(consoleEntry({
        kind: "step", icon: "✂", time, tag: "orchestrator",
        label: `${p.split.length} step(s) over ${p.threshold} points broken up`,
        body: () => el("pre", null, (p.split || []).map(
          (s) => `${s.points} pts: ${s.task}\n` +
                 s.into.map((t) => `   → ${t}`).join("\n")).join("\n\n")),
      }));
      return;

    case "memory_compacted":
      consolePush(consoleEntry({
        kind: "step", icon: "🧠", time, tag: "memory",
        label: p.compacted
          ? `compacted project memory: ${p.before} notes → ${p.after}`
          : `memory left as it was — ${p.reason}`,
        body: () => el("pre", null, (p.notes || []).join("\n")
                       + (p.archive ? `\n\noriginals kept in ${p.archive}` : "")),
      }));
      loadMemory();
      return;

    case "fixed":
      consolePush(consoleEntry({
        kind: "write", icon: "🛠", time, tag: event.agent,
        label: labelWith([
          ["fix applied", ""],
          [(p.files || []).length ? `  ${p.files.join(", ")}` : "  no files changed", "c-path"],
        ]),
        body: () => el("pre", null, p.summary || ""),
      }));
      return;

    case "run_stopped":
      if (!p.aborted_model_calls) return;
      consolePush(consoleEntry({
        kind: "cmd", icon: "⏹", time, tag: "system", failed: true,
        label: p.message || "stopped — the model call in flight was broken off",
      }));
      return;

    case "supervision": case "warning": case "error":
      consolePush(consoleEntry({
        kind: "cmd", icon: ICON.fail, time, failed: true, tag: event.agent || "system",
        label: clip(p.message || p.error || "", 90),
        body: () => el("pre", null, [p.message, p.traceback].filter(Boolean).join("\n\n")),
        open: true,
      }));
      return;

    case "step_finished": case "step_failed":
      consolePush(consoleEntry({
        kind: event.type === "step_failed" ? "cmd" : "write",
        icon: event.type === "step_failed" ? ICON.fail : ICON.verdict, time,
        failed: event.type === "step_failed", tag: event.agent,
        label: `step ${event.type === "step_failed" ? "failed" : p.status || "done"}` +
               ((p.files || []).length ? ` — ${p.files.join(", ")}` : ""),
        body: () => el("pre", null, p.summary || p.reason || ""),
      }));
      return;

    default:
      return;   // everything else lives in the step detail, not the console
  }
}

$("show-reads").onchange = () => {
  // Replay the current step so the reads appear or disappear in place.
  const box = $("console");
  box.innerHTML = "";
  const events = state.events.filter((e) => !consoleStep || e.step_id === consoleStep);
  if (!events.length) return consoleReset();
  events.forEach(consoleAppend);
};

/* ───────────── live command entries: elapsed, cancel, allow ────────── */

const liveCommands = new Map();   // command_id -> { entry, elapsedEl, started }
let liveTicker = null;

function tickLiveCommands() {
  const now = Date.now();
  liveCommands.forEach((live) => {
    live.elapsedEl.textContent = `${Math.round((now - live.started) / 1000)}s`;
  });
  if (!liveCommands.size && liveTicker) {
    clearInterval(liveTicker);
    liveTicker = null;
  }
}

function pushRunningCommand(event) {
  const p = event.payload || {};
  const entry = el("div", "c-entry c-cmd c-running");
  const head = el("div", "c-head");
  head.append(el("span", "c-time", new Date(event.ts).toLocaleTimeString()));
  head.append(el("span", "c-icon spin", "◐"));
  head.append(el("span", "c-label", p.command));

  const elapsedEl = el("span", "c-elapsed", "0s");
  head.append(elapsedEl,
              el("span", "c-tag", p.background ? "background" : `limit ${p.timeout_s}s`));

  const cancel = el("button", "copy-btn", "cancel");
  cancel.title = "Kill this command and anything it started";
  cancel.onclick = async (e) => {
    e.stopPropagation();
    cancel.textContent = "cancelling…";
    cancel.disabled = true;
    let r;
    try {
      r = await api(`/api/commands/cancel/${encodeURIComponent(p.command_id)}`,
                    { method: "POST" });
    } catch (_) {
      // Never leave the button stuck on "cancelling…".
      cancel.textContent = "cancel";
      cancel.disabled = false;
      return;
    }
    if (r.cancelled) {
      cancel.textContent = "cancelled";
    } else {
      cancel.textContent = "cancel";
      cancel.disabled = false;
      toast("Nothing to cancel — it had already finished.");
    }
  };
  head.append(cancel);
  entry.append(head);

  liveCommands.set(p.command_id, { entry, elapsedEl, started: Date.now() });
  if (!liveTicker) liveTicker = setInterval(tickLiveCommands, 1000);
  consolePush(entry);
}

function finishRunningCommand(event) {
  const live = liveCommands.get(event.payload?.command_id);
  if (!live) return;
  liveCommands.delete(event.payload.command_id);
  live.entry.remove();          // the tool_call entry replaces it with the result
  tickLiveCommands();
}

/** Offer to allow the programs a refusal named. */
function allowButton(detail) {
  const programs = detail.programs || [];
  const scope = detail.agent_has_own_list ? `${detail.agent}'s list` : "the allowlist";
  const btn = el("button", "copy-btn allow-btn", `allow ${programs.join(", ")}`);
  btn.title = `Add to ${scope} and let the agent try again`;
  btn.onclick = async (e) => {
    e.preventDefault();
    e.stopPropagation();
    const r = await api("/api/commands/allow", {
      method: "POST",
      body: { programs, agent: detail.agent },
    });
    btn.textContent = "allowed";
    btn.disabled = true;
    toast(r.scope === "agent"
      ? `Added to ${r.agent}'s allowlist — it applies on the next call.`
      : `Added to the global allowlist — it applies on the next call.`);
  };
  return btn;
}
