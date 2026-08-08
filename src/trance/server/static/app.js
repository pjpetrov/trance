"use strict";

const state = {
  session: null,
  roles: {},
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

/* One badge, coloured by what the status means rather than by which word it is:
 * green for working, amber for waiting on you, red for stopped by a fault, and
 * grey for over. Used in the session list and the session bar, so the same
 * status never looks like two different things. */
function statusBadge(status) {
  const known = ["running", "paused", "error", "planning", "ready", "finished"];
  const badge = el("span",
    `badge status-${known.includes(status) ? status : "ready"}`, status || "?");
  badge.title = {
    running: "an agent is working on this right now",
    paused: "waiting for you — a question, or the pause button",
    error: "the run stopped on a fault",
    planning: "no flow yet: still talking to the orchestrator",
    ready: "planned, not running",
    finished: "every step is done",
  }[status] || "";
  return badge;
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
    left.append(el("div", "muted small", s.project_dir));

    const right = el("div", "row");
    right.append(statusBadge(s.status));
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
  trackClock(state.session);
  // A question asked before this page existed is still blocking an agent.
  api(`/api/sessions/${id}/approvals`).then((data) => {
    (data.pending || []).forEach((request) => consolePush(approvalEntry({
      type: "approval_requested", agent: request.agent, step_id: request.step_id,
      payload: { ...request, timeout_s: data.timeout_s },
    })));
  }).catch(() => {});
  state.events = [];
  consoleStep = null;
  consoleReset();
  resetFiles();
  // Before the socket, not alongside it. Fired off in parallel, the history
  // lands *after* whatever the socket has already delivered — old entries
  // appended below new ones, the console's scope set back to an older step,
  // and the next live event clearing the lot. History first, then live.
  await loadConsoleTail(id);
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
             statusBadge(state.session.status));
  const plan = el("button", null, "Plan");
  plan.onclick = () => show("plan");
  const run = el("button", null, "Run");
  run.onclick = () => { show("run"); renderRun(); };
  const files = el("button", null, "Files");
  files.onclick = () => openFiles();
  const pending = (state.session.review || []).length;
  if (pending) files.append(el("span", "badge review-count", String(pending)));
  bar.append(plan, run, files);
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
    // The response is a snapshot taken when the request returned. Splitting
    // runs after that and pushes a newer flow over the websocket, so assigning
    // the whole session here can put the un-split plan back — the split really
    // happened, and the screen silently went backwards.
    const before = state.flowVersion || 0;
    const body = await api(`/api/sessions/${state.session.id}/chat`, {
      method: "POST", body: { message: text },
    });
    const newer = (state.flowVersion || 0) !== before;
    const flow = state.session.flow;
    const team = state.session.team;
    state.session = body;
    if (newer) {
      state.session.flow = flow;
      state.session.team = team;
    }
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
  // The step picker offers loops, so their names have to be loaded.
  if (!state.loops) api("/api/loops").then((d) => { state.loops = d; redrawEditor(); })
                                     .catch(() => { state.loops = { loops: [] }; });
  state.draftSteps = JSON.parse(JSON.stringify(state.session?.flow?.steps || []));
  state.draftBase = draftFingerprint();   // what the server last gave us
  state.planStale = false;
  const box = $("flow-editor");
  box.innerHTML = "";
  if (!state.draftSteps.length) {
    box.append(el("p", "muted small",
      "No steps yet — describe the project to the orchestrator, or add one manually."));
  }
  // Finished steps are kept now rather than replaced by a new plan, so the list
  // grows. Hiding them is a view, not an edit: they stay in the flow, in git and
  // in the count below.
  const hide = $("hide-finished") && $("hide-finished").checked;
  let hidden = 0;
  state.draftSteps.forEach((step, index) => {
    if (hide && TERMINAL.has(step.status)) { hidden += 1; return; }
    box.append(stepCard(step, index));
  });
  if (hidden) {
    const note = el("p", "muted small");
    const show = el("button", "small ghost", `show ${hidden} finished step(s)`);
    show.onclick = () => { $("hide-finished").checked = false; redrawEditor(); };
    note.append(show);
    box.prepend(note);
  }
}

//: Only the parts a user edits — status and attempts change under them.
function draftFingerprint() {
  return JSON.stringify((state.draftSteps || []).map(
    (s) => [s.role, s.task, s.check, s.on_fail, s.max_loops]));
}

//: Steps the orchestrator thinks are too big — pointed out, not acted on.
function splittingIds() {
  return new Set((state.oversized && state.oversized.step_ids) || []);
}

/* A note, not a spinner. Splitting rewrites the plan you are reading and costs
 * a model call, and "too big" is a judgement: five points may be exactly the
 * step you meant. So it says so, and the split button next to it is yours. */
function splittingMark(step) {
  const mark = el("span", "splitting-mark");
  mark.append(el("span", null, "large"));
  mark.title = `Estimated over ${(state.oversized || {}).threshold || "the"} points. `
               + "Press split if you want the orchestrator to break it up.";
  return mark;
}

//: What an agent gets before a step gives up on it: its own tries, plus the
//: ones on its backup when it has one.
function agentTotalTries(name) {
  const agent = state.roles[name];
  if (!agent) return 2;
  const main = Math.max(1, agent.tries ?? 2);
  return main + (agent.backup_preset ? Math.max(0, agent.backup_tries ?? 2) : 0);
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
    if (splittingIds().has(step.id)) head.append(splittingMark(step));
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
    makeDraggable(card, index);
    return card;
  }

  // One picker for both: a step runs an agent or a loop, never a bit of each.
  const roleSelect = el("select");
  Object.keys(state.roles).forEach((name) => {
    const opt = el("option", null, name);
    opt.value = `agent:${name}`;
    if (!step.loop && name === step.role) opt.selected = true;
    roleSelect.append(opt);
  });
  ((state.loops && state.loops.loops) || []).forEach((loop) => {
    const opt = el("option", null, `↻ ${loop.name}`);
    opt.value = `loop:${loop.name}`;
    opt.title = loop.description || "";
    if (step.loop === loop.name) opt.selected = true;
    roleSelect.append(opt);
  });
  roleSelect.disabled = !editable;
  roleSelect.onchange = () => {
    const [kind, value] = roleSelect.value.split(":");
    if (kind === "loop") {
      step.loop = value;
      // A loop carries its own wiring; the step's check/fixer/limit would be a
      // second, contradictory answer to the same question.
      step.check = null;
      step.on_fail = null;
    } else {
      step.loop = "";
      step.role = value;
    }
    redrawEditor();
    queueFlowSave(true);
  };

  head.append(roleSelect);
  if (step.status && step.status !== "pending") {
    const tag = el("span", "badge", step.status);
    if (!editable) tag.title = "This step is running — edit it once it settles";
    head.append(tag);
  }

  head.append(pointsBadge(step));
  if (splittingIds().has(step.id)) head.append(splittingMark(step));

  // Splitting is the fix for the number being too big, so it belongs next to it.
  const split = el("button", "small", "split");
  split.disabled = !editable || !state.session;
  split.title = "Ask the orchestrator to break this into smaller steps";
  split.onclick = () => splitStep(step, split);
  head.append(split);

  const remove = el("button", null, "✕");
  remove.disabled = !editable;
  remove.onclick = () => {
    state.draftSteps.splice(index, 1);
    redrawEditor();
    queueFlowSave(true);
  };
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
  task.oninput = () => { step.task = task.value; queueFlowSave(); };

  // Three separate controls: the reality check, who fixes a failure, and how
  // many times the block may loop before the run is halted.
  const gatesBox = el("div", "gates");
  const drawGates = () => {
    gatesBox.innerHTML = "";
    if (step.loop) {
      const loop = ((state.loops && state.loops.loops) || [])
        .find((l) => l.name === step.loop);
      const note = el("div", "row small muted");
      note.append(el("span", null,
        `↻ ${step.loop} — ${loop ? loop.roles.join(" → ") : "not found"}. `
        + "Its own wiring decides who runs and when it stops."));
      const edit = el("button", "small", "edit loop");
      edit.onclick = (e) => { e.preventDefault(); openLoops(); };
      note.append(edit);
      gatesBox.append(note);
      return;
    }

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
    check.onchange = () => { step.check = check.value || null; drawGates(); queueFlowSave(true); };

    // No "on fail" agent here any more. Handing a failure to a *different*
    // agent is what a loop is, and having two ways to say it left the step
    // editor pretending to a power it could only half express.
    // Blank means the agent's own count, which is the usual case: the agent
    // knows what it is worth retrying, the step only overrides when it differs.
    const agentTries = agentTotalTries(step.role);
    const loops = el("input", "compact tiny");
    loops.type = "number";
    loops.min = 1;
    loops.value = step.max_loops || "";
    loops.placeholder = String(agentTries);
    loops.disabled = !editable;
    loops.title = `Blank uses ${step.role || "the agent"}'s own count (${agentTries}). `
                  + "Set a number to override it for this step alone.";
    loops.onchange = () => {
      step.max_loops = Number(loops.value) || 0;
      queueFlowSave();
      drawGates();
    };

    const revert = el("input");
    revert.type = "checkbox";
    revert.checked = !!step.revert_on_fail;
    revert.disabled = !editable;
    revert.onchange = () => {
      step.revert_on_fail = revert.checked; drawGates(); queueFlowSave(true);
    };
    const revertLabel = el("label", "row small");
    revertLabel.append(revert, document.createTextNode(" revert on failure"));
    revertLabel.title = "Put the project back to where it was before this step ran, "
                        + "if the step does not succeed. The work stays in git history.";

    row.append(field("check", check), field("tries", loops));
    // Blank is the common case, so say whose number is filling it in.
    if (!step.max_loops) row.append(el("span", "hint", `${step.role}'s own`));
    row.append(revertLabel);
    if (step.on_fail) {
      // A flow built before loops existed. Say what it will do, and let it go.
      const legacy = el("span", "badge", `on fail → ${step.on_fail}`);
      legacy.title = "Set before loops existed. Clear it to have this agent simply retry.";
      const clear = el("button", "small", "clear");
      clear.disabled = !editable;
      clear.onclick = () => { step.on_fail = null; drawGates(); queueFlowSave(true); };
      row.append(legacy, clear);
    }
    gatesBox.append(row);

    const limit = step.max_loops || agentTries;
    gatesBox.append(el("div", "loop-note",
      `${step.role} reports SUCCESS → next step. Anything else → ${step.role} tries `
      + `again (${limit} tr${limit > 1 ? "ies" : "y"} in all, then the flow halts). `
      + "To bring another agent in on failure, run a loop instead."));
    if (step.revert_on_fail) {
      gatesBox.append(el("div", "loop-note",
        `↩ If ${step.role} does not succeed, its changes are rolled back to the `
        + "checkpoint taken before it ran. The attempt is still committed first, "
        + "so nothing is lost — it is undone, not destroyed."));
    }
    gatesBox.append(el("div", step.check ? "loop-note check-note" : "loop-note muted",
      step.check
        ? `${step.check} separately checks that the report is true. If ${step.role} `
          + `claims SUCCESS and ${step.check} disagrees, ${step.role} is told what was `
          + "missing and tries again; the run halts only if it never becomes true."
        : `No fact check — ${step.role}'s report of its own outcome is taken at face value.`));
  };
  drawGates();

  card.append(head, task, gatesBox);

  makeDraggable(card, index);
  return card;
}

/* Dragging a step used to be a guess: the card you were over got the step, but
 * "over" says nothing about whether it lands above or below. So the edge it
 * would land on is drawn as a line, and that same edge is what the drop uses —
 * one decision, shown and then acted on. */
function makeDraggable(card, index) {
  card.addEventListener("dragstart", (e) => {
    card.classList.add("dragging");
    e.dataTransfer.setData("text/plain", String(index));
    e.dataTransfer.effectAllowed = "move";
  });
  card.addEventListener("dragend", () => {
    card.classList.remove("dragging");
    clearDropMarks();
  });
  card.addEventListener("dragover", (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const before = isAboveMiddle(card, e);
    if (card.classList.contains(before ? "drop-before" : "drop-after")) return;
    clearDropMarks();
    card.classList.add(before ? "drop-before" : "drop-after");
  });
  card.addEventListener("dragleave", (e) => {
    // Only when the pointer has really left: moving over a child fires this too.
    if (!card.contains || !e.relatedTarget || !card.contains(e.relatedTarget)) {
      card.classList.remove("drop-before", "drop-after");
    }
  });
  card.addEventListener("drop", (e) => {
    e.preventDefault();
    const before = isAboveMiddle(card, e);
    card.classList.remove("drop-before", "drop-after");   // this one, for certain
    clearDropMarks();                                      // and any other that stuck
    const from = Number(e.dataTransfer.getData("text/plain"));
    if (Number.isNaN(from)) return;

    let to = before ? index : index + 1;
    if (from === to || from === to - 1) return;   // already there
    const [moved] = state.draftSteps.splice(from, 1);
    if (from < to) to -= 1;                       // the removal shifted everything after it
    state.draftSteps.splice(to, 0, moved);
    queueFlowSave(true);
    redrawEditor();
  });
}

//: Above the middle of the card means "goes before this one".
function isAboveMiddle(card, event) {
  if (!card.getBoundingClientRect) return true;
  const box = card.getBoundingClientRect();
  return (event.clientY || 0) < box.top + box.height / 2;
}

function clearDropMarks() {
  const box = $("flow-editor");
  const marked = box && box.querySelectorAll
    ? box.querySelectorAll(".drop-before, .drop-after") : [];
  Array.from(marked).forEach((n) => n.classList.remove("drop-before", "drop-after"));
}

function redrawEditor() {
  const box = $("flow-editor");
  box.innerHTML = "";
  // Finished steps are kept now rather than replaced by a new plan, so the list
  // grows. Hiding them is a view, not an edit: they stay in the flow, in git and
  // in the count below.
  const hide = $("hide-finished") && $("hide-finished").checked;
  let hidden = 0;
  state.draftSteps.forEach((step, index) => {
    if (hide && TERMINAL.has(step.status)) { hidden += 1; return; }
    box.append(stepCard(step, index));
  });
  if (hidden) {
    const note = el("p", "muted small");
    const show = el("button", "small ghost", `show ${hidden} finished step(s)`);
    show.onclick = () => { $("hide-finished").checked = false; redrawEditor(); };
    note.append(show);
    box.prepend(note);
  }
}

$("hide-finished").onchange = () => redrawEditor();

$("collapse-editor").onchange = () => {
  state.openSteps.clear();      // the toggle is the master switch
  redrawEditor();
};

/* Ask for a plan without having to phrase it. The message is sent through the
 * ordinary chat, and shows up there, because a plan that appeared with nothing
 * in the conversation explaining it is a plan you cannot argue with. */
$("generate-plan").onclick = async () => {
  if (!state.session) return;
  const said = (state.session.chat || []).some((m) => m.role === "user");
  if (!said && !(state.session.goal || "").trim()) {
    return toast("Tell the orchestrator what you are building first — it has "
                 + "nothing to plan from yet.");
  }
  const button = $("generate-plan");
  button.disabled = true;
  button.textContent = "planning…";
  try {
    $("chat-text").value = state.draftSteps.length
      ? "Propose the plan again, from everything we have discussed. Replace the "
        + "current steps."
      : "Propose the plan now, from everything we have discussed.";
    await sendChat();
  } finally {
    button.disabled = false;
    button.textContent = "Generate plan";
  }
};

/* Emptying the flow. Confirmed, because the steps are work — yours or the
 * orchestrator's — and there is no undo beyond asking for another plan. */
$("clear-plan").onclick = async () => {
  if (!state.session || !state.draftSteps) return;
  const n = state.draftSteps.length;
  if (!n) return toast("The plan is already empty.");
  const sure = await confirmDialog(
    `Clear all ${n} step(s)?`,
    "The flow is emptied and saved. A step that is running right now is kept — "
    + "stop the run first if you want that gone too.");
  if (!sure) return;
  state.draftSteps = [];
  redrawEditor();
  await saveFlowNow();
  // The server keeps anything mid-flight, so show what actually survived.
  state.draftSteps = JSON.parse(JSON.stringify(state.session?.flow?.steps || []));
  state.draftBase = draftFingerprint();
  redrawEditor();
  toast(state.draftSteps.length
    ? `Cleared — ${state.draftSteps.length} running step(s) kept.` : "Plan cleared.");
};

$("add-step").onclick = () => {
  state.draftSteps.push({ role: "backend", loop: "", task: "", check: null, on_fail: null,
                          max_loops: 2, status: "pending" });
  redrawEditor();
  queueFlowSave(true);
  // A new step goes on the end, so put it in view rather than making the user
  // scroll past everything that is already done.
  const box = $("flow-editor");
  box.scrollTop = box.scrollHeight;
  const last = box.lastElementChild?.querySelector("textarea");
  if (last) last.focus();
};

/* Edits save themselves. A Save button is a way to lose work: the plan is the
 * one screen you leave to go and look at something, and an unsaved flow that
 * looks saved is worse than no flow at all.
 *
 * Debounced, because typing a task is one edit, not forty. Structural changes —
 * adding, deleting, reordering — go immediately, since there is nothing more
 * coming and they are the ones worth not losing. */
let flowSaveTimer = null;

function queueFlowSave(now = false) {
  if (!state.session || !isPlanning()) return;
  clearTimeout(flowSaveTimer);
  flowSaveTimer = setTimeout(saveFlowNow, now ? 0 : 700);
}

async function saveFlowNow() {
  clearTimeout(flowSaveTimer);
  if (!state.session || !state.draftSteps) return;
  const sending = JSON.stringify(state.draftSteps);
  markFlowSaving("saving…");
  try {
    const flow = await api(`/api/sessions/${state.session.id}/flow`, {
      method: "PUT", body: { steps: JSON.parse(sending) },
    });
    state.session.flow = flow;
    // Not renderFlowEditor(): redrawing under the cursor would move it, and
    // what came back is what we just sent. Only the baseline needs updating,
    // so a server-side change can still be told apart from a local edit.
    state.draftBase = draftFingerprint();
    const n = (flow.requeued || []).length;
    markFlowSaving(n ? `saved — ${n} step(s) re-queued` : "saved");
  } catch (_) {
    markFlowSaving("not saved — check the connection");
  }
}

function markFlowSaving(what) {
  const mark = $("flow-saved");
  if (!mark) return;
  mark.textContent = what;
  mark.className = "muted small" + (what.startsWith("not saved") ? " err" : "");
}

$("start-run").onclick = async () => {
  // Whatever is still in the debounce goes now, not in 700ms from a screen you
  // have already left.
  await saveFlowNow();
  state.session = await api(`/api/sessions/${state.session.id}/start`, { method: "POST" });
  show("run");
  renderRun();
};


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
  status.append(runClock());
  status.append(spendBadge());
  if (session.error) status.append(el("span", "badge", session.error));

  renderFlowView();
  paintPaused();
  loadMemory();      // only the count; the notes load when the modal opens
}

/* ──────────────────── hinting a working agent ─────────────────────── */

/* The moment you notice a wrong assumption is while it is still being acted
 * on. A hint goes to whatever is running and reaches it on its next round —
 * no pausing, no picking the exact line it came from. */

async function sendHint() {
  const box = $("hint-text");
  const note = box.value.trim();
  if (!note || !state.session) return;
  const button = $("hint-send");
  button.disabled = true;
  try {
    const result = await api(`/api/sessions/${state.session.id}/steer`,
                             { method: "POST", body: { note } });
    box.value = "";
    toast(result.delivering
      ? "Sent — the agent sees it on its next round."
      : "Queued — it goes in when that step starts.");
  } catch (_) {
    // The message says why; leave the text so it is not lost.
  } finally {
    button.disabled = false;
  }
}

$("hint-send").onclick = sendHint;
$("hint-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendHint();
});

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

/* ───────────────────────── how long it has run ─────────────────────── */

/* A total, not a stopwatch. A flow that was stopped twice and restarted did not
 * take a fresh five minutes — it took the sum of what it spent, and that is the
 * number worth knowing. The server banks each stretch; the browser only ticks
 * the one in progress. */

const clock = { base: 0, since: 0, working: false };
let clockTimer = null;

function trackClock(session) {
  clock.base = Number(session.run_seconds) || 0;
  clock.working = !!session.working;
  clock.since = Date.now();
  paintClock();
  if (clock.working && !clockTimer) clockTimer = setInterval(paintClock, 1000);
  if (!clock.working && clockTimer) {
    clearInterval(clockTimer);
    clockTimer = null;
  }
}

function clockSeconds() {
  return clock.base + (clock.working ? (Date.now() - clock.since) / 1000 : 0);
}

/* What this run has asked of each model. Tokens, not money: trance does not
 * know anybody's price list, and a number invented from a stale table would be
 * believed. The breakdown is per model because that is the decision it informs
 * — which model to point the next agent at. */
function spendBadge() {
  const node = el("span", "muted small spend");
  const paint = (body) => {
    node.innerHTML = "";
    if (!body || !body.models.length) return;
    node.append(el("span", null, `${tokens(body.total)} tok`));
    node.title = body.models
      .map((m) => `${m.model}: ${m.calls} call(s), ${tokens(m.input_tokens)} in / `
                  + `${tokens(m.output_tokens)} out`)
      .join("\n");
  };
  if (state.session) {
    api(`/api/sessions/${state.session.id}/usage`).then(paint).catch(() => {});
  }
  return node;
}

//: 23,350 -> "23.4k". A token count is a size, not an amount to reconcile.
function tokens(n) {
  n = Number(n) || 0;
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 100_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

function runClock() {
  const node = el("span", "run-clock");
  node.id = "run-clock";
  node.title = "Total time this flow has been working, across every start, "
               + "pause and restart. It stops when the flow does.";
  paintClock(node);
  return node;
}

function paintClock(node) {
  const box = node || document.getElementById("run-clock");
  if (!box) return;
  const total = Math.round(clockSeconds());
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const text = h ? `${h}h ${String(m).padStart(2, "0")}m`
    : m ? `${m}m ${String(seconds).padStart(2, "0")}s`
    : `${seconds}s`;
  box.innerHTML = "";
  box.classList.toggle("ticking", clock.working);
  box.append(el("span", "muted small", "worked"), el("b", null, text));
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

/* Editing replaces the cards rather than appearing under them: the raw file and
 * the rendered notes are two views of the same thing, and showing both invites
 * editing one while reading the other. */
function memoryEditing(on) {
  $("memory-editor").hidden = !on;
  $("memory-list").hidden = on;
  $("memory-edit").hidden = on;
  $("memory-compact").hidden = on;
  $("memory-save").hidden = !on;
  $("memory-cancel").hidden = !on;
  if (on) $("memory-raw").focus();
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

$("memory-cancel").onclick = () => {
  renderMemory();              // discard the edit, restore the cards as they were
  memoryEditing(false);
};

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
/* The plan screen holds a draft you may be halfway through editing. An update
 * from the server is worth taking, but not at the cost of what you have typed:
 * unsaved edits keep the screen and get told, rather than being overwritten. */
function refreshPlan() {
  if (draftFingerprint() === state.draftBase) {
    const showing = JSON.stringify((state.draftSteps || []).map((s) => s.id));
    const server = JSON.stringify((state.session?.flow?.steps || []).map((s) => s.id));
    if (showing !== server) renderFlowEditor();
    return;
  }
  if (!state.planStale) {
    state.planStale = true;
    toast("The flow changed — your unsaved edits are still here. Save or "
          + "discard them to take the new steps.");
  }
}

function applyRefinedFlow(payload) {
  state.oversized = null;
  if (!state.session) return;
  //: Bumped on every flow the server pushes, so a slower response cannot put
  //: an older one back.
  state.flowVersion = (state.flowVersion || 0) + 1;
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

/* The console's own history, asked for rather than pushed.
 *
 * The socket used to replay the whole session on connect — for a long run that
 * is thousands of events and tens of megabytes of prompts, arriving one at a
 * time, with everything else waiting behind them. Now the socket carries what
 * happens next, and each screen fetches the part of the past it needs. */
async function loadConsoleTail(sessionId) {
  let body;
  try {
    body = await api(`/api/sessions/${sessionId}/events?tail=true`);
  } catch (_) { return; }
  if (!state.session || state.session.id !== sessionId) return;   // moved on

  (body.events || []).forEach((event) => {
    state.events.push(event);
    consoleAppend({ ...event, replay: true });
  });
  if (body.total > body.shown) {
    consolePush(consoleEntry({
      kind: "step", icon: "…", time: "",
      label: `${body.total - body.shown} earlier event(s) not shown — open a step `
             + `to see its own history.`,
    }));
  }
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
      trackClock(state.session);
      paintPaused();
      if (!["running", "paused"].includes(state.session.status)) clearActivity();
      renderRun();
      renderSessionBar();
      // The run view reads the session directly; the plan screen edits a copy
      // of it, so a step added elsewhere — a review sent, the orchestrator
      // finishing — never reached it. Take the new flow when there is nothing
      // of yours to lose, and say so when there is.
      if (isPlanning()) refreshPlan();
      return;
    }
    // Replayed history rebuilds the console and nothing else. It describes a
    // moment that has passed, so anything it says about the current flow, the
    // running agent or a pending question is already out of date.
    state.events.push(event);
    if (event.replay) {
      consoleAppend(event);
      return;
    }

    if (event.type === "approval_requested") setActivity(
      event.payload.agent, "waiting for you to allow or refuse an action");
    if (event.type === "oversized_steps") {
      state.oversized = event.payload;
      if (isPlanning()) renderFlowEditor();
    }
    if (event.type === "flow_updated" && event.payload.flow) {
      applyRefinedFlow(event.payload);
    }
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
      return `${shortModel(p.model, p.preset)} · round ${p.round} · ${p.summary?.est_tokens ?? "?"} tok in · ` +
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
    case "git": return `${p.message || p.action}${p.sha ? ` (${p.sha.slice(0, 8)})` : ""}`;
    case "check_failed": return p.message || "";
    case "model_switched": return p.message || "";
    case "steering_delivered": return `hint delivered: “${clip(p.note, 70)}”`;
    case "loop_node": return p.message || "";
    case "loop_exhausted": return p.message || "";
    case "oversized_steps": return p.message;
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
    case "delegated": return p.message || "running inside Claude Code";
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

/* A model id, shortened. Some backends have none — Claude Code takes whatever
 * its CLI is signed in to — so the name you gave the model stands in. "?" was
 * accurate and useless. */
function shortModel(model, preset) {
  const name = model || preset || "";
  if (!name) return "?";
  const tail = name.split("/").pop();
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
  state.presets = info.presets;
  state.planning = info.planning || { max_step_points: 0, scale: [1, 2, 3, 5, 8, 13] };
  state.orchestrator = info.orchestrator;
  state.kinds = info.kinds;
  await loadWorkspace();
  await loadHome();
})();

/* Esc closes whatever is open, topmost first. Every modal had its own ✕ and
 * its own backdrop click; none of them had the key everyone reaches for. */
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  const open = Array.from(document.querySelectorAll(".modal.open"));
  const top = open[open.length - 1];
  if (!top) return;
  // Leave a half-written note alone: Esc in a textarea means "undo my typing"
  // to the browser, and closing the dialog under it loses more than it saves.
  const active = document.activeElement;
  if (active && active.tagName === "TEXTAREA" && top.contains(active)) return;
  top.classList.remove("open");
  e.preventDefault();
});

/* ─────────────────────────── settings modal ───────────────────────── */

$("open-settings").onclick = openSettings;
$("close-settings").onclick = () => $("settings").classList.remove("open");
$("settings").onclick = (e) => { if (e.target.id === "settings") e.currentTarget.classList.remove("open"); };

async function openSettings() {
  $("settings").classList.add("open");
  // These used to be called by the provider renderer, which no longer exists.
  // Deleting it took the models list and the orchestrator picker with it.
  await renderPresets();
  renderOrchestratorSettings();
  renderGitSettings();
}

/* Settings that are still settings. Step size and escalation went with the
 * automatic splitting: both read like instructions for behaviour that no longer
 * happens on its own. The values behind them survive — max_step_points is what
 * marks a step "large" and what the split button argues against — they are just
 * not presented as knobs on a screen that would then be describing a machine
 * that is not running. */
function renderGitSettings() {
  const git = $("git-commits");
  const autoInit = $("git-auto-init");
  const planning = state.planning || {};
  git.checked = planning.git_commits !== false;
  autoInit.checked = planning.git_auto_init !== false;
  const saveGit = async () => {
    const body = await api("/api/config/planning", {
      method: "PUT",
      body: { git_commits: git.checked, git_auto_init: autoInit.checked },
    });
    state.planning = { ...state.planning, ...body };
    toast(body.git_commits ? "Committing after every step."
                           : "Not committing — steps cannot be reverted.");
  };
  git.onchange = saveGit;
  autoInit.onchange = saveGit;
}

async function renderPresets() {
  const { presets } = await api("/api/presets");
  state.presets = presets;
  paintPresets();
}

/* Same shape as the agents: the list is what you have, the pane is the one you
 * are changing. A model is a form — endpoint, key, model id — and several of
 * them stacked is a page you scroll rather than a thing you read. */
function paintPresets(selected) {
  const presets = state.presets || [];
  if (selected !== undefined) state.presetSelected = selected;
  if (!presets.some((m) => m.name === state.presetSelected)) {
    state.presetSelected = presets.length ? presets[0].name : "";
  }

  const names = $("preset-names");
  names.innerHTML = "";
  presets.forEach((preset) => {
    const row = el("button", "agent-name"
      + (preset.name === state.presetSelected ? " on" : ""));
    row.append(el("span", "agent-name-text", preset.name));
    const spend = preset.spend;
    row.append(el("span", "muted small",
      spend ? `${tokens(spend.total)} tok` : KIND_SHORT[preset.kind] || preset.kind || ""));
    row.title = `${preset.model || "no model id"} — ${preset.base_url || "default endpoint"}`
      + (spend ? `\n${spend.calls} call(s), ${tokens(spend.input_tokens)} in / `
                 + `${tokens(spend.output_tokens)} out, all time` : "");
    row.onclick = () => paintPresets(preset.name);
    names.append(row);
  });

  const box = $("preset-list");
  box.innerHTML = "";
  const showing = presets.find((m) => m.name === state.presetSelected);
  if (showing) box.append(presetCard(showing, false));
  else box.append(el("p", "muted small", "No models defined yet — add one on the right."));
}

//: Enough to tell two rows apart without spelling out the API each time.
const KIND_SHORT = { anthropic: "anthropic", openai: "openai",
                     ollama: "ollama", llamacpp: "llama.cpp" };

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
  const baseUrl = el("input", "compact");
  baseUrl.value = preset.base_url || "";
  baseUrl.placeholder = "default for this API";
  baseUrl.title = "The root, not the full path — trance appends /chat/completions "
                  + "for every OpenAI-compatible API. So https://host/v1, not "
                  + "https://host/v1/chat/completions.";

  const key = el("input", "compact");
  key.type = "password";
  key.value = preset.has_key ? "***" : "";
  key.placeholder = preset.has_key ? "saved — leave to keep" : "none needed locally";
  key.title = "Sent as the API key. Leave the dots alone to keep the stored one.";

  // A list where the endpoint offers one, free text where it does not. Never
  // a closed dropdown: a listing that misses a model would then lock it out.
  const model = el("input", "compact");
  model.value = preset.model || "";
  model.placeholder = "model id";
  const suggestions = el("datalist");
  suggestions.id = `models-${preset.name || "new"}-${Math.random().toString(36).slice(2, 7)}`;
  model.setAttribute("list", suggestions.id);
  const modelNote = el("span", "hint", "");

  let discovering = null;
  const discover = async () => {
    const body = {
      name: preset.name, kind: kind.value, base_url: baseUrl.value.trim(),
      ...(key.value && key.value !== "***" ? { api_key: key.value.trim() } : {}),
    };
    const signature = JSON.stringify(body);
    if (discovering === signature) return;
    discovering = signature;
    modelNote.textContent = "asking the endpoint what it can run…";
    let found;
    try {
      found = await api("/api/models/discover", { method: "POST", body });
    } catch (_) {
      modelNote.textContent = "could not ask — type the model id";
      return;
    }
    suggestions.innerHTML = "";
    (found.models || []).forEach((id) => {
      const option = el("option");
      option.value = id;
      suggestions.append(option);
    });
    modelNote.textContent = found.listed
      ? `${found.models.length} available — start typing, or pick from the list`
      : `this endpoint does not list its models (${found.note}) — type the id`;
    // One model and nothing chosen yet is not a choice worth making by hand.
    if (found.listed && !model.value && found.models.length === 1) {
      model.value = found.models[0];
    }
  };

  const wrap = (label, node) => {
    const l = el("label", null, label);
    l.append(node);
    return l;
  };
  const wrapHint = (label, node, hint) => {
    const l = wrap(label, node);
    l.append(el("span", "hint", hint));
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

  // Changing the API moves the endpoint with it, so a llama.cpp URL is not
  // left behind on a model switched to Anthropic.
  kind.onchange = () => {
    const spec = (state.kinds || {})[kind.value] || {};
    baseUrl.placeholder = spec.base_url || "default for this API";
    if (!baseUrl.value || Object.values(state.kinds || {}).some(
        (k) => k.base_url === baseUrl.value)) {
      baseUrl.value = "";
    }
    ctx.placeholder = spec.context_window ? String(spec.context_window) : "default";
    localOnly();
    if (kind.value === "claudecode") { baseUrl.value = ""; key.value = ""; }
    else discover();
  };
  // Typing a URL or pasting a key changes the answer, so ask again — once the
  // typing stops, not on every keystroke.
  let discoverTimer;
  const rediscover = () => {
    clearTimeout(discoverTimer);
    discoverTimer = setTimeout(discover, 600);
  };
  baseUrl.addEventListener("input", rediscover);
  key.addEventListener("input", rediscover);

  // Held as a variable rather than found by position: the discovery note and
  // the suggestion list both live in this label.
  const modelField = wrap("Model id", model);
  modelField.append(suggestions, modelNote);

  const urlField = wrapHint("Base URL", baseUrl, "the root — /chat/completions is added");
  const keyField = wrap("API key", key);
  grid.append(wrap("Name (agents pick this)", name), wrap("API", kind),
              urlField, keyField,
              modelField, wrap("Context window", ctx),
              wrap("Max output", out));

  // Claude Code has neither: it runs the `claude` binary on this machine and
  // bills against whatever that CLI is logged in to. Asking for an endpoint
  // and a key would be asking for something that cannot exist.
  const localOnly = () => {
    const runsLocally = kind.value === "claudecode";
    urlField.hidden = runsLocally;
    keyField.hidden = runsLocally;
    modelNote.textContent = runsLocally
      ? "Empty means whatever the CLI is signed in to — usually opus, the most "
        + "expensive. Type sonnet or haiku to spend less on ordinary steps; a "
        + "delegated step is one long call, so the model's rate applies to all "
        + "of it."
      : modelNote.textContent;
  };

  const spec = (state.kinds || {})[kind.value] || {};
  baseUrl.placeholder = spec.base_url || "default for this API";
  ctx.placeholder = spec.context_window ? String(spec.context_window) : "default";
  localOnly();
  if (kind.value !== "claudecode") discover();

  const actions = el("div", "row small");
  const probe = el("button", null, "Test");
  const probeResult = el("span", "check-result");
  probe.title = "Send a one-token request, so a wrong key or URL surfaces here "
                + "rather than mid-run";
  probe.onclick = async () => {
    probeResult.textContent = "checking…";
    probeResult.className = "check-result";
    const body = await api(`/api/presets/${encodeURIComponent(preset.name)}/check`,
                           { method: "POST" });
    probeResult.textContent = body.ok
      ? `ok — ${body.reply || body.model}`
      : `${body.error}\ncalled: ${body.endpoint || "?"}`;
    probeResult.title = `called ${body.endpoint || "?"}`;
    probeResult.className = `check-result ${body.ok ? "ok" : "bad"}`;
  };

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
        base_url: baseUrl.value.trim(),
        // "***" means "keep what is stored"; the server drops it.
        ...(key.value !== "***" ? { api_key: key.value.trim() } : {}),
        model: model.value.trim(),
        context_window: Number(ctx.value) || 0,
        max_tokens: Number(out.value) || 0,
      },
    });
    if (target === preset.name) toast(`Saved model “${target}”.`);
    state.presetSelected = target;        // stay on the one you just edited
    await renderPresets();
    await refreshConfig();
    if (state.session) state.session = await api(`/api/sessions/${state.session.id}`);
  };
  actions.append(save);

  if (isNew) {
    const cancel = el("button", null, "Cancel");
    cancel.onclick = () => paintPresets(state.presets?.[0]?.name || "");
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
    actions.append(probe, probeResult, del);
  }

  card.append(grid, actions);
  return card;
}

function renderOrchestratorSettings() {
  const box = $("orchestrator-settings");
  box.innerHTML = "";
  const select = el("select", "compact");
  if (!(state.presets || []).length) {
    box.append(el("p", "muted small", "No models defined yet — add one above."));
    return;
  }
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
  state.presets = info.presets;
  state.orchestrator = info.orchestrator;
  state.kinds = info.kinds;
  paintStale(info.stale);
}

/* A server running code older than what is on disk answers with yesterday's
 * behaviour and no sign of it — every "it is still broken" costs a round trip
 * to work out. It can see this for itself, so it says so. */
function paintStale(stale) {
  const bar = $("session-bar");
  if (!bar) return;
  const existing = bar.querySelector(".stale-note");
  if (existing && existing.remove) existing.remove();
  if (!stale) return;
  const note = el("span", "badge stale-note", "trance itself was updated — restart it");
  note.title = "This is about trance, not your project. Its own source files have "
               + "changed since this server started, so you are still using the "
               + "version that was running when you launched it. Stop trance and "
               + "start it again to pick up the change; your sessions are on disk "
               + "and survive.";
  bar.append(note);
}

$("add-preset").onclick = () => {
  // A new model brings its own endpoint. Nothing else has to exist first.
  const kind = Object.keys(state.kinds || {})[0] || "llamacpp";
  state.presetSelected = "";
  $("preset-names").querySelectorAll(".agent-name").forEach(
    (row) => row.classList.remove("on"));
  const box = $("preset-list");
  box.innerHTML = "";
  box.append(presetCard({ name: "", kind, base_url: "", model: "", provider: "" }, true));
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
  // The card offers a model and a command list, so both have to be here first.
  if (!state.commands) {
    try { state.commands = await api("/api/commands"); } catch (_) { /* offline */ }
  }
  try {
    state.presets = (await api("/api/presets")).presets;
  } catch (_) { /* keep whatever boot loaded */ }
  const data = await api("/api/agents");
  state.toolsets = data.toolsets;
  state.agents = data.agents;
  paintAgents();
}

/* One agent at a time. Eight cards of remits, prompts and models stacked in a
 * modal is a page you scroll looking for the one you came for; the list on the
 * right is the whole team at a glance, and the space goes to the agent you
 * picked. */
function paintAgents(selected) {
  const agents = state.agents || [];
  if (selected !== undefined) state.agentSelected = selected;
  if (!agents.some((a) => a.name === state.agentSelected)) {
    state.agentSelected = agents.length ? agents[0].name : "";
  }

  const names = $("agent-names");
  names.innerHTML = "";
  agents.forEach((agent) => {
    const row = el("button", "agent-name"
      + (agent.name === state.agentSelected ? " on" : ""));
    const dot = el("span", "agent-dot");
    dot.style.background = agent.color || "#7aa2f7";
    row.append(dot, el("span", "agent-name-text", agent.name));
    if (agent.verifier) row.append(el("span", "muted small", "✓"));
    row.title = agent.description || agent.title || agent.name;
    row.onclick = () => paintAgents(agent.name);
    names.append(row);
  });

  const box = $("agent-list");
  box.innerHTML = "";
  const showing = agents.find((a) => a.name === state.agentSelected);
  if (showing) box.append(agentCard(showing, false));
  else box.append(el("p", "muted small", "No agents yet — add one on the right."));
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
  if (agent.backup_preset) {
    head.append(el("span", "badge",
                   `${agent.tries ?? 2} tries, then ${agent.backup_tries ?? 2} on `
                   + `${agent.backup_preset}`));
  }
  if (agent.verifier) head.append(el("span", "badge", "verifier"));
  if (agent.resolved) {
    head.append(el("span", "muted small",
      `→ ${agent.resolved.model} (${Number(agent.resolved.context_window).toLocaleString()} ctx)`));
  }

  // --- model -----------------------------------------------------------
  const grid = el("div", "agent-fields");
  const wrap = (label, node, hint) => {
    const l = el("label", null, label);
    l.append(node);
    if (hint) l.append(el("span", "hint", hint));
    return l;
  };
  //: One field per line: a label of fixed width, the control taking the rest,
  //: and anything trailing kept beside it rather than wrapping under.
  const rowField = (label, node, trailing) => {
    const row = el("label", "agent-row");
    row.append(el("span", "agent-label", label), node);
    if (trailing) row.append(trailing);
    return row;
  };

  // Only models that exist. "default model" meant an endpoint nobody had
  // defined — with providers gone there is no such thing, and picking it sent
  // the agent at a localhost that may not be running.
  const preset = el("select", "compact");
  const models = state.presets || [];
  if (!models.length) {
    const none = el("option", null, "no models defined — add one in ⚙");
    none.value = "";
    preset.append(none);
    preset.disabled = true;
  }
  models.forEach((m) => {
    const opt = el("option", null, `${m.name} — ${m.model}`);
    opt.value = m.name;
    if (m.name === agent.preset) opt.selected = true;
    preset.append(opt);
  });
  // An agent saved before, pointing at a model since deleted, would otherwise
  // show whatever happened to be first while still holding the old name.
  if (models.length && !models.some((m) => m.name === agent.preset)) {
    preset.value = models[0].name;
  }
  // Model and backup are the same decision made twice, so they stack rather
  // than sitting in grid columns that squeeze both into half a name.
  // The backup: what this agent moves to when the same model keeps failing.
  const backup = el("select", "compact");
  const noBackup = el("option", null, "none — keep trying the same model");
  noBackup.value = "";
  backup.append(noBackup);
  models.forEach((m) => {
    const opt = el("option", null, `${m.name} — ${m.model}`);
    opt.value = m.name;
    if (m.name === agent.backup_preset) opt.selected = true;
    backup.append(opt);
  });

  const tries = el("input", "compact tiny");
  tries.type = "number";
  tries.min = "1";
  tries.value = agent.tries ?? 2;
  tries.title = "Attempts on the model above before the backup takes over.";

  const backupTries = el("input", "compact tiny");
  backupTries.type = "number";
  backupTries.min = "0";
  backupTries.value = agent.backup_tries ?? 2;
  backupTries.title = "Attempts on the backup after that. The two added together are "
                      + "what a step gets unless the step says otherwise.";

  // Each model sits on its own line with the number of tries that belongs to
  // it, and the total is stated underneath — the question is "how many goes
  // does this agent get on which model", and this is that sentence.
  const triesWrap = el("span", "row small after-tries");
  triesWrap.append(el("span", "muted small", "tries"), tries);
  const backupWrap = el("span", "row small after-tries");
  backupWrap.append(el("span", "muted small", "tries"), backupTries);

  const total = el("div", "tries-total");
  const syncBackup = () => {
    backupWrap.hidden = !backup.value;
    const main = Math.max(1, Number(tries.value) || 1);
    const extra = backup.value ? Math.max(0, Number(backupTries.value) || 0) : 0;
    total.innerHTML = "";
    total.append(el("b", null, String(main + extra)),
                 el("span", "muted", backup.value
                   ? ` tries in all — ${main} on the model, then ${extra} on the backup.`
                   : " tries in all. Add a backup model to get more."),
                 el("span", "muted", " A step can override this."));
  };
  backup.onchange = syncBackup;
  tries.addEventListener("input", syncBackup);
  backupTries.addEventListener("input", syncBackup);
  syncBackup();

  grid.append(rowField("Model", preset, triesWrap),
              rowField("Backup model", backup, backupWrap),
              total);

  // How many tool rounds one attempt gets. It used to be twelve for everyone,
  // set in code — and an agent that runs out mid-file reports what it meant to
  // do rather than what it did.
  const rounds = el("input", "compact tiny");
  rounds.type = "number";
  rounds.min = "0";
  rounds.value = agent.tool_rounds || "";
  rounds.placeholder = "12";
  const roundsNote = el("span", "muted small");
  const sayRounds = () => {
    const n = Number(rounds.value) || 0;
    roundsNote.textContent = n
      ? `${n} reads, writes or commands per attempt, then it must report.`
      : "12 by default — raise it for an agent that builds a feature file by file.";
  };
  rounds.addEventListener("input", sayRounds);
  sayRounds();
  grid.append(rowField("Tool rounds", rounds, roundsNote));

  const description = el("input", "compact");
  description.value = agent.description || "";
  description.placeholder = "what this agent is for — the orchestrator reads this";
  grid.append(rowField("Description", description));

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
  const cmdRow = el("div", "agent-fields");
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
  cmdRow.append(rowField("Command list", listSel),
                rowField("Own commands", commands),
                rowField("Pipes / redirects", shellSel),
                rowField("Run commands in", workdir));
  perms.append(cmdRow);

  const paths = el("textarea");
  paths.rows = 3;
  paths.value = (agent.paths || []).join("\n");
  paths.placeholder = "backend/**\n*.py";
  perms.append(el("div", "muted small",
                  "Remit — one glob per line. Writes outside these are refused."));
  perms.append(paths);

  // An empty box is a decision, and a meaningful one: it reads as unfinished
  // otherwise, which is how it ended up being refused at save time.
  const remitNote = el("div", "muted small remit-note");
  const sayRemit = () => {
    const globs = paths.value.split("\n").map((g) => g.trim()).filter(Boolean);
    remitNote.textContent = globs.length
      ? `Can write to ${globs.length} path pattern(s). Reads are not restricted.`
      : "Read-only: it can read and list every file, and every write is refused.";
  };
  paths.addEventListener("input", sayRemit);
  sayRemit();
  perms.append(remitNote);

  // --- prompt ----------------------------------------------------------
  const promptWrap = el("details");
  promptWrap.append(el("summary", "muted small", "▸ system prompt"));
  const prompt = el("textarea");
  prompt.rows = 10;
  prompt.value = agent.system_prompt || "";
  promptWrap.append(prompt);

  // A draft to edit, not a finished prompt: the model knows the shape these
  // need, and you know what the agent is actually for.
  const draftRow = el("div", "row small");
  const draft = el("button", null, "✎ write one from the name");
  draft.title = "Ask the orchestrator's model for a first draft, from this agent's "
                + "name and description. It replaces what is in the box.";
  draft.onclick = async () => {
    const shortname = name.value.trim();
    if (!shortname) return toast("Give it a name first — that is what the draft is about.");
    const was = prompt.value;
    draft.disabled = true;
    draft.textContent = "writing…";
    try {
      const drafted = await api("/api/agents/draft-prompt", {
        method: "POST",
        body: { name: shortname, description: description.value.trim(),
                session: state.session ? state.session.id : "" },
      });
      prompt.value = drafted.system_prompt;
      const undo = el("button", null, "undo");
      undo.onclick = () => { prompt.value = was; undo.remove(); };
      draftRow.append(undo);
      toast("Drafted — read it before saving. It is a starting point.");
    } finally {
      draft.disabled = false;
      draft.textContent = "✎ write one from the name";
    }
  };
  draftRow.append(draft);
  promptWrap.append(draftRow);
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
      backup_preset: backup.value || null,
      tries: Math.max(1, Number(tries.value) || 2),
      tool_rounds: Math.max(0, Number(rounds.value) || 0),
      backup_tries: Math.max(0, Number(backupTries.value) || 0),
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
    state.agentSelected = shortname;      // stay on the one you just edited
    await renderAgents();
    await refreshConfig();
    if (state.session) state.session = await api(`/api/sessions/${state.session.id}`);
  };
  actions.append(save);

  if (isNew) {
    const cancel = el("button", null, "Cancel");
    cancel.onclick = () => paintAgents(state.agents?.[0]?.name || "");
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

/* A blank prompt box teaches nothing about what belongs in one. This is the
 * shape of every prompt that works here — who it is, what it does, and the two
 * things agents get wrong without being told — with the parts to replace marked
 * so it is obvious they are yours and not a default that already fits. */
const AGENT_TEMPLATE = [
  "You are the «WHAT THIS AGENT IS», working on «THE PART OF THE PROJECT IT OWNS».",
  "",
  "What you do:",
  "- «THE ONE THING IT IS FOR — be specific: 'write the HTTP handlers', not 'do backend work'»",
  "- «WHAT IT MUST NEVER DO — the mistake you expect from it»",
  "",
  "How to work:",
  "- Read before you write: get_definition on a symbol beats reading a whole file.",
  "- Change what the task asks for and nothing else.",
  "- «ANYTHING THIS PROJECT DOES DIFFERENTLY — a framework, a convention, a test command»",
  "",
  "Report OUTCOME: SUCCESS only when the work is done and you have checked it.",
  "Report OUTCOME: FAILED — why, when it is not. A wrong success costs the next agent more",
  "than an honest failure.",
].join("\n");

$("add-agent").onclick = () => {
  // A new one takes over the pane, so there is only ever one card on screen.
  state.agentSelected = "";
  $("agent-names").querySelectorAll(".agent-name").forEach(
    (row) => row.classList.remove("on"));
  const box = $("agent-list");
  box.innerHTML = "";
  box.append(agentCard({
    name: "", title: "", description: "", system_prompt: AGENT_TEMPLATE,
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
    const badge = el("span", "badge role", step.loop ? `↻ ${step.loop}` : step.role);
    if (step.loop) badge.classList.add("loop-badge");
    else if (role) badge.style.background = role.color;
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
    if (step.loop) {
      const loop = ((state.loops && state.loops.loops) || [])
        .find((l) => l.name === step.loop);
      meta.append(el("div", "flow-chain",
        `↻ ${loop ? loop.roles.join(" → ") : step.loop} — the loop's wiring decides `
        + "who runs next and when it stops"));
    } else {
      if (step.checker) {
        meta.append(el("div", "flow-chain",
          `↳ fact-checked by ${step.checker} · a false report is sent back, then halts`));
      }
      meta.append(el("div", "flow-chain",
        step.on_fail
          ? `↻ not success → ${step.on_fail} fixes → ${step.role} again `
            + `(${step.loop_limit} tries)`
          : `↻ not success → ${step.role} tries again `
            + `(${step.loop_limit} tr${step.loop_limit > 1 ? "ies" : "y"} in all)`));
    }
    const attempts = step.attempts || [];
    if (attempts.length) {
      const last = attempts[attempts.length - 1];
      const parts = [`attempt ${last.n}`];
      if (last.outcome) parts.push(`outcome ${last.outcome}`);
      (last.gate_results || []).forEach((g) => parts.push(`${g.gate}:${g.verdict}`));
      if (last.files_written?.length) parts.push(last.files_written.join(", "));
      const row = el("div", "row small step-usage");
      row.append(el("span", "muted small", parts.join(" · ")));
      // What this step actually cost. The live gauge disappears with the step
      // that was running; keeping the last reading is how you compare them.
      const used = lastContext(step);
      if (used) row.append(contextGauge(used));
      meta.append(row);
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
  const startRerun = async (onBackup) => {
    let r;
    try {
      r = await api(`/api/sessions/${state.session.id}/steps/${step.id}/rerun`,
                    { method: "POST", body: { on_backup: onBackup } });
    } catch (_) { return; }
    // A stop only lands when the model call returns, so "nothing happened" is
    // usually "waiting" — say which, or the button reads as broken.
    const resumed = r.resumed ? " Un-paused." : "";
    const where = onBackup ? " on the backup model" : "";
    toast(r.restarted ? `Re-running ${step.role}${where}…${resumed}`
          : r.waiting_for ? `${step.role} queued${where} — starts when ${r.waiting_for}.${resumed}`
          : `${step.role} queued${where} — the running engine will pick it up.${resumed}`);
  };

  const rerun = el("button", null, "rerun");
  rerun.title = "Run this step again from the start, on its usual model";
  rerun.onclick = () => startRerun(false);

  // Straight to the backup: you have watched the usual model fail and know it
  // will again, so spending its tries first is only slower.
  const backupOf = state.roles[step.role]?.backup_preset;
  if (backupOf && !step.loop) {
    const rerunBig = el("button", null, `rerun on ${backupOf}`);
    rerunBig.title = `Skip the tries on ${step.role}'s usual model and start on `
                     + `${backupOf} straight away`;
    rerunBig.onclick = () => startRerun(true);
    head.append(rerun, rerunBig);
  } else {
    head.append(rerun);
  }
  const skip = el("button", null, "skip");
  skip.onclick = () => api(`/api/sessions/${state.session.id}/steps/${step.id}/skip`, { method: "POST" });
  head.append(skip);
  if (step.loop) head.append(el("span", "badge loop-badge", `↻ ${step.loop}`));
  if (step.checker) head.append(el("span", "badge", `check: ${step.checker}`));
  if (step.on_fail) head.append(el("span", "badge", `on fail: ${step.on_fail}`));
  // What this step cost, where you look when you wonder why it went wrong.
  const used = lastContext(step);
  if (used) {
    const gauge = contextGauge(used);
    gauge.style.marginLeft = "auto";
    head.append(gauge);
  }
  body.append(head);

  body.append(rowWithCopy("task", step.task || ""));
  body.append(el("pre", "code", step.task || "(no task)"));

  if (step.summary) {
    body.append(rowWithCopy("result", step.summary));
    body.append(el("pre", "code", step.summary));
  }

  // One section per block that ran. A step with five loop passes was a single
  // wall of model calls; which pass a call belonged to was the one thing you
  // needed and the only thing not shown.
  const history = el("div");
  body.append(history);
  paintStepHistory(history, step);
}

/* The step's own events, fetched if the browser does not have them.
 *
 * It used to read whatever the websocket had replayed — and a long run is
 * thousands of events, so a page that loaded after one of those showed a step
 * with no history at all. Its events were on the server the whole time. */
async function paintStepHistory(box, step) {
  // Two rules, because a detail panel that flickers is worse than one that is
  // slow. Never show less than is already up, and never let a slow answer
  // overwrite a newer one — clicking two steps quickly used to leave whichever
  // request happened to land last.
  const mine = ++openedStep;
  let shown = -1;

  const paint = (events, waiting) => {
    if (mine !== openedStep) return;             // a different step is open now
    const blocks = groupStepEvents(events, step);
    // Counted in what will be on screen, not in events: a set that groups into
    // fewer sections is a worse answer however many events it contains.
    const weight = blocks.reduce((n, b) => n + (b.events || []).length, 0);
    if (weight < shown) return;                  // never go backwards
    shown = weight;
    box.innerHTML = "";
    blocks.forEach((block, i) => box.append(
      blockSection(block, i === blocks.length - 1 && step.status === "running")));
    if (waiting) box.append(el("p", "muted small", waiting));
    else if (!blocks.length) {
      box.append(el("p", "muted small",
        "Nothing recorded for this step. It may have been skipped, or it ran "
        + "before this session kept a trace."));
    }
  };

  const held = state.events.filter((e) => e.step_id === step.id);
  paint(held, "Loading this step's history…");

  let fetched = [];
  try {
    fetched = await api(
      `/api/sessions/${state.session.id}/events?step=${encodeURIComponent(step.id)}`);
  } catch (_) { /* keep whatever the console had */ }
  paint(Array.isArray(fetched) && fetched.length >= held.length ? fetched : held, "");
}

//: Bumped each time a step is opened, so an answer for the previous one is
//: dropped instead of painted over the current.
let openedStep = 0;

/* Split a step's events into the blocks that produced them. Each block starts
 * where the engine says one starts — a step attempt, a loop node, a fixer, an
 * escalation — so the grouping is the engine's own, not a guess from timing. */
const BLOCK_STARTS = new Set(["step_started", "loop_node", "fixing", "escalated"]);

function groupStepEvents(events, step) {
  const attempts = step.attempts || [];
  const blocks = [];
  let taken = 0;

  events.forEach((event) => {
    if (BLOCK_STARTS.has(event.type) || !blocks.length) {
      const p = event.payload || {};
      const isAttempt = event.type === "step_started" || event.type === "loop_node";
      blocks.push({
        agent: event.agent || step.role,
        kind: event.type === "fixing" ? "fix"
          : event.type === "escalated" ? "escalation"
          : event.type === "loop_node" ? "loop" : "attempt",
        label: p.message || (p.task ? `attempt ${p.attempt || blocks.length + 1}` : ""),
        n: p.visit || p.attempt || blocks.length + 1,
        attempt: isAttempt ? attempts[taken++] : null,
        events: [],
      });
    }
    blocks[blocks.length - 1].events.push(event);
  });

  // A step whose events predate this page still has its attempt records.
  if (!blocks.length) {
    attempts.forEach((a) => blocks.push({
      agent: step.role, kind: "attempt", label: "", n: a.n, attempt: a, events: [],
    }));
  }
  return blocks;
}

function blockSection(block, openByDefault) {
  const wrap = el("details", "step-block");
  wrap.open = !!openByDefault;

  const summary = el("summary");
  const badge = el("span", "badge role", block.agent);
  const role = state.roles[block.agent];
  if (role) badge.style.background = role.color;
  summary.append(el("span", "block-n", `${block.n}.`), badge);

  const a = block.attempt;
  if (block.kind === "fix") summary.append(el("span", "badge", "fixing"));
  if (block.kind === "escalation") summary.append(el("span", "badge", "escalated"));

  // Folded, this line is the whole point: what came of this block. Its own
  // step_outcome wins over the step's attempt list: a loop makes more blocks
  // than attempts, so the two line up only by luck, and when they disagree the
  // event was emitted by this block and the attempt belongs to another.
  const said = (block.events || []).find((e) => e.type === "step_outcome");
  const outcome = said ? (said.payload.outcome || said.payload.exit) : (a && a.outcome);
  if (outcome) {
    summary.append(el("span",
      `badge outcome-${outcome === "SUCCESS" ? "ok" : "bad"}`, outcome));
  }
  (a ? a.gate_results || [] : []).forEach((g) => {
    summary.append(el("span", `badge outcome-${g.verdict === "PASS" ? "ok" : "bad"}`,
                      `${g.gate}: ${g.verdict}`));
  });

  // A loop makes more blocks than the step has attempts, so most of them had no
  // attempt to take an outcome from and showed none — the one thing you open a
  // block to find out. Each block's own outcome and verdict are in its events.
  const checked = (block.events || []).find((e) => e.type === "verdict");
  if (checked) {
    const verdict = checked.payload.verdict || checked.payload.result || "?";
    const badge = el("span", `badge outcome-${verdict === "PASS" ? "ok" : "bad"}`,
                     `${checked.payload.verifier || checked.agent || "check"}: ${verdict}`);
    badge.title = checked.payload.reason || checked.payload.detail || "";
    summary.append(badge);
  }

  const reason = (said && (said.payload.reason || said.payload.message))
                 || (a && (a.outcome_reason || a.feedback));
  if (reason) summary.append(el("span", "muted small block-why", clip(reason, 80)));
  else if (a && (a.files_written || []).length) {
    summary.append(el("span", "muted small", a.files_written.join(", ")));
  }
  if (a && a.context && a.context.window) summary.append(contextGauge(a.context));
  wrap.append(summary);

  // Built when the section is opened, not before. One block of a long step is
  // hundreds of events; building every block of every attempt up front is how
  // opening a step stopped the tab rather than filling it.
  const inner = el("div", "step-block-body");
  wrap.append(inner);
  let built = false;
  const build = () => {
    if (built) return;
    built = true;
    fill(inner);
  };
  wrap.addEventListener("toggle", () => { if (wrap.open) build(); });
  if (wrap.open) build();
  return wrap;

  function fill(inner) {
  if (a && a.outcome_reason) {
    inner.append(rowWithCopy("why the step failed", a.outcome_reason));
    inner.append(el("pre", "code", a.outcome_reason));
  }
  if (a && a.feedback) {
    inner.append(rowWithCopy("fact check", a.feedback));
    inner.append(el("pre", "code", a.feedback));
  }

  const bundles = block.events.filter((e) => e.type === "context_bundle");
  const calls = block.events.filter((e) => e.type === "model_call");
  const tools = block.events.filter((e) => e.type === "tool_call");
  bundles.forEach((b) => {
    const d = el("details");
    d.append(el("summary", "muted small",
      `▸ curated bundle: ${b.payload.stats?.symbols ?? 0} symbols, `
      + `~${b.payload.stats?.est_tokens ?? 0} tok`));
    d.append(el("pre", "code", b.payload.rendered || ""));
    inner.append(d);
  });
  if (calls.length) {
    inner.append(el("h3", null, `Context — ${calls.length} model call(s)`));
    calls.forEach((c) => inner.append(renderEvent(c)));
  }
  if (tools.length) {
    inner.append(el("h3", null, `Tool calls — ${tools.length}`));
    tools.forEach((t) => inner.append(renderEvent(t)));
  }
  if (!inner.children.length) {
    // An attempt with no events is almost never an attempt that did nothing —
    // it is one whose trace was never written. Saying "no calls" invites a hunt
    // for a bug in the panel; saying why ends it.
    inner.append(el("p", "muted small",
      "Nothing was recorded for this attempt. It ran before this session kept a "
      + "trace on disk, so its history existed only in the page that was open at "
      + "the time."));
  }
  }
  return wrap;
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
  // Not every console entry is a collapsible line — a permission prompt is a
  // form. Assuming one here threw inside the websocket handler, so the card
  // never rendered and the run looked stuck waiting for a button that was
  // never drawn.
  const head = entry.querySelector(".c-head");
  if (!head) return;
  entry.dataset.ref = "1";
  entry.classList.toggle("interceptable", isPaused());

  head.addEventListener("click", (e) => {
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

  const picker = $("cmd-names");
  picker.innerHTML = "";
  names.forEach((name) => {
    const row = el("button", "agent-name" + (name === current ? " on" : ""));
    row.append(el("span", "agent-name-text", name));
    if (name === data.default) row.append(el("span", "muted small", "default"));
    const size = ((data.lists || {})[name] || {}).allowed || [];
    row.title = size.length ? `${size.length} program(s)` : "empty";
    row.onclick = () => renderCommands(name);
    picker.append(row);
  });
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

//: The most recent window reading this step produced, if it ever ran.
function lastContext(step) {
  const attempts = step.attempts || [];
  for (let i = attempts.length - 1; i >= 0; i--) {
    const ctx = attempts[i].context;
    if (ctx && ctx.window) return ctx;
  }
  return null;
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

/* One line, in the header, wherever you are. It used to be drawn twice — once
 * there and once on the run screen — which just made the same fact compete
 * with itself for space. */
function paintActivity() {
  const header = $("now-working");
  if (!activity.agent) {
    header.className = "now-working";
    header.innerHTML = "";
    return;
  }
  const role = state.roles[activity.agent];
  const since = elapsed(Date.now() - activity.since);

  header.className = "now-working active";
  header.innerHTML = "";
  header.append(el("span", "dot"));

  // Which step, taken from the flow rather than from the events, so this and
  // the marker on the card cannot disagree. Three steps running the same loop
  // look identical otherwise, and "the reviewer is working" says nothing about
  // which of them it is working on.
  const steps = state.session?.flow?.steps || [];
  const at = steps.findIndex((s) => s.status === "running");
  if (at >= 0) {
    const where = el("span", "activity-step", `step ${at + 1}/${steps.length}`);
    where.title = steps[at].task || "";
    header.append(where);
  }

  const badge = el("span", "badge role", activity.agent);
  if (role) badge.style.background = role.color;
  header.append(badge,
                el("span", "activity-what", clip(activity.what, 52)),
                el("span", "activity-elapsed", since));
  if (activity.model) header.append(el("span", "muted small", shortModel(activity.model)));
  else if (activity.preset) header.append(el("span", "muted small", activity.preset));
  if (activity.context) header.append(contextGauge(activity.context));
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
      // The name you gave it, when the backend has no id of its own to report.
      activity.preset = p.preset || "";
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
    // A delegated step is one long call with nothing coming back until it ends.
    // Without this the header kept whatever it last knew — usually "indexing" —
    // so a step that was working looked like one that had hung.
    case "delegated":
      activity.context = null;
      setActivity(event.agent, "running the whole step inside Claude Code",
                  p.model); break;
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
  // ...and the filter with it. A cleared console that still only accepts the
  // last step's events drops everything else without saying so.
  consoleStep = null;
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
  // finished step stays available by clicking it in the pipeline. A loop step
  // never emits step_started, so scoping on that alone left the console showing
  // the previous step while the loop ran, and silently dropped everything the
  // loop did, up to and including a permission prompt the run was blocked on.
  if (event.type === "step_started" || event.type === "loop_node") {
    const fresh = event.step_id !== consoleStep;
    consoleStep = event.step_id;
    if (fresh) box.innerHTML = "";
    $("console-scope").textContent = event.type === "loop_node"
      ? `${event.agent} · ${p.loop} block ${p.visit || 1}`
      : `${event.agent} · attempt ${p.attempt || 1}`;
    consolePush(consoleEntry({
      kind: "step", icon: event.type === "loop_node" ? "↻" : ICON.step, time,
      tag: event.agent,
      label: labelWith([[clip(p.message || p.task, 90), ""]]),
    }));
    return;
  }
  // A question the run is blocked on is never out of scope: dropping it leaves
  // an agent waiting for a button that was filtered away.
  const blocking = event.type === "approval_requested" || event.type === "approval_resolved";
  if (!blocking && event.step_id && consoleStep && event.step_id !== consoleStep) return;

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
      if (d.kind === "edit_miss" || d.kind === "edit_ambiguous") {
        // Not a refusal and not a failure of the file — the agent described a
        // snippet that is not there, or is there twice.
        consolePush(consoleEntry({
          kind: "read", icon: "✎", time, tag: event.agent, failed: true,
          label: labelWith([
            [`edit did not apply `, ""], [d.path || d.symbol || "", "c-path"],
            [d.count ? `  ${d.count} matches` : "  no match", "muted"],
          ]),
          body: () => el("pre", null, p.result || ""),
          open: true,
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
          kind: "think", icon: "?", time, tag: shortModel(p.model, p.preset),
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
        kind: "think", icon: ICON.think, time, tag: shortModel(p.model, p.preset),
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
                          [`  ${p.role} on ${shortModel(p.model, p.preset)}`, "c-path"]]),
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

    case "loop_node":
      consolePush(consoleEntry({
        kind: "step", icon: "↻", time, tag: event.agent,
        label: labelWith([[p.message || "", ""],
                          [p.focus ? `  ${clip(p.focus, 60)}` : "", "muted"]]),
      }));
      return;

    case "loop_exhausted":
      consolePush(consoleEntry({
        kind: "cmd", icon: "↻", time, tag: event.agent, failed: true,
        label: p.message || "the loop is not converging",
      }));
      return;

    case "check_failed":
      consolePush(consoleEntry({
        kind: "cmd", icon: "↺", time, tag: event.agent, failed: true,
        label: p.message || "the check disagreed",
        body: () => el("pre", null, p.detail || ""),
        open: true,
      }));
      return;

    case "steering":
    case "steering_delivered":
      consolePush(consoleEntry({
        kind: "step", icon: "✎", time, tag: "you",
        label: labelWith([
          [event.type === "steering_delivered" ? "hint delivered — " : "hint queued — ", ""],
          [clip(p.note, 90), ""],
        ]),
        open: true,
      }));
      return;

    case "model_switched":
      consolePush(consoleEntry({
        kind: "step", icon: "⇧", time, tag: event.agent,
        label: labelWith([[p.message || "switching model", ""],
                          [`  ${shortModel(p.from)} → ${shortModel(p.to)}`, "c-path"]]),
        open: true,
      }));
      return;

    case "git": {
      const icon = p.action === "revert" ? "↩" : p.action === "init" ? "⎇" : "⌥";
      consolePush(consoleEntry({
        kind: p.action === "revert" ? "cmd" : "write", icon, time,
        tag: event.agent || "git", failed: p.ok === false,
        label: labelWith([
          [p.message || p.action, ""],
          [p.sha ? `  ${p.sha.slice(0, 8)}` : "", "c-path"],
        ]),
        body: () => el("pre", null, (p.files || []).join("\n") || p.detail || ""),
        open: p.action === "revert",
      }));
      return;
    }

    case "oversized_steps":
      consolePush(consoleEntry({
        kind: "step", icon: "✂", time, tag: "orchestrator",
        label: p.message || "some steps came out large",
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

    case "delegated":
      consolePush(consoleEntry({
        kind: "step", icon: "⇥", time, tag: event.agent,
        label: `handed to Claude Code — one call, trance's tools over MCP `
               + `(${p.model || "default"})`,
        body: () => el("pre", null, p.message || ""),
      }));
      return;

    // A reply cut at the output limit is the most expensive thing that can go
    // wrong quietly: minutes of generation, nothing written, and the console
    // showed a tool call that simply did not appear. Say it, loudly.
    case "truncated":
      consolePush(consoleEntry({
        kind: "cmd", icon: "✂", time, failed: true, tag: event.agent || "system",
        label: `reply cut at the ${p.limit || "output"}-token limit`
               + (p.attempt > 1 ? ` (${p.attempt} in this step)` : ""),
        body: () => el("pre", null,
          (p.message || "") + "\n\nThe agent was told to write in pieces instead: "
          + "edit_file for part of a file, replace_symbol for one function, or "
          + "write_file then append_file."),
        open: true,
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

/* ═══════════════════════ loops: blocks of agents ═══════════════════ */

/* A loop is a small state machine, and the honest way to edit one is to show
 * every block with a row per outcome. Each row is "when this happens → go
 * there, at most N times". No canvas, no dragging: the arrows are the data, and
 * a dropdown cannot point somewhere that does not exist. */

const OUTCOME_LABEL = {
  SUCCESS: "on SUCCESS",
  FAILED: "on FAILED",
  CHECK_FAILED: "on CHECK FAILED",
};

$("open-loops").onclick = openLoops;
$("close-loops").onclick = () => $("loops").classList.remove("open");
for (const id of ["close-preview-warning", "preview-cancel"]) {
  $(id).onclick = () => $("preview-warning").classList.remove("open");
}
$("loops").onclick = (e) => {
  if (e.target.id === "loops") e.currentTarget.classList.remove("open");
};

async function openLoops() {
  $("loops").classList.add("open");
  await renderLoops();
}

/* One loop at a time. A loop is a state machine with a row per outcome per
 * block; three of them stacked is more than anyone can hold at once, and the
 * one you came to edit is never the one on screen. */
async function renderLoops(select) {
  const data = await api("/api/loops");
  state.loops = data;
  const names = data.loops.map((l) => l.name);
  const current = names.includes(select) ? select
    : names.includes(state.loopName) ? state.loopName : names[0];
  state.loopName = current;

  // The same shape as the agents and the models: what you have down the side,
  // the one you are editing in the pane. A dropdown hides how many there are.
  const picker = $("loop-names");
  picker.innerHTML = "";
  data.loops.forEach((entry) => {
    const row = el("button", "agent-name" + (entry.name === current ? " on" : ""));
    row.append(el("span", "agent-name-text", entry.name));
    row.append(el("span", "muted small", `${entry.nodes.length}`));
    row.title = entry.description || (entry.roles || []).join(" → ");
    row.onclick = () => renderLoops(entry.name);
    picker.append(row);
  });

  const box = $("loop-list");
  box.innerHTML = "";
  const loop = data.loops.find((l) => l.name === current);
  $("loop-summary").textContent = loop
    ? `${loop.nodes.length} block(s) · ${loop.roles.join(" → ")}`
    : "";
  if (!loop) {
    box.append(el("p", "muted small",
      "No loops yet. A loop is a block of agents wired by outcome — start with "
      + "the agent that runs first."));
    return;
  }
  box.append(loopCard(structuredClone(loop), false));
}

/* A copy with its own block ids. Reusing the originals would leave the copy's
 * arrows pointing at blocks in the loop it came from — which reads correctly on
 * screen and is wrong the moment either is edited. */
function cloneLoop(loop) {
  const copy = structuredClone(loop);
  copy.name = uniqueLoopName(loop.name);
  const renamed = {};
  copy.nodes.forEach((node) => {
    renamed[node.id] = `n_${Math.random().toString(36).slice(2, 8)}`;
  });
  copy.nodes.forEach((node) => {
    node.id = renamed[node.id];
    Object.values(node.on || {}).forEach((routes) => {
      (Array.isArray(routes) ? routes : [routes]).forEach((edge) => {
        if (renamed[edge.target]) edge.target = renamed[edge.target];
      });
    });
  });
  copy.start = renamed[loop.start] || (copy.nodes[0] ? copy.nodes[0].id : "");
  return copy;
}

//: "test-and-fix" -> "test-and-fix-2", and past whatever already exists.
function uniqueLoopName(name) {
  const taken = new Set(((state.loops && state.loops.loops) || []).map((l) => l.name));
  const base = name.replace(/-\d+$/, "");
  for (let n = 2; n < 100; n++) {
    if (!taken.has(`${base}-${n}`)) return `${base}-${n}`;
  }
  return `${base}-copy`;
}

function loopCard(loop, isNew) {
  const card = el("div", "provider-card loop-card");
  const meta = state.loops || { agents: [], verifiers: [], outcomes: [] };

  const head = el("div", "provider-grid");
  const name = el("input", "compact");
  name.value = loop.name || "";
  name.placeholder = "e.g. test-and-fix";
  name.disabled = !isNew;
  const description = el("input", "compact");
  description.value = loop.description || "";
  description.placeholder = "what this loop is for";
  const steps = el("input", "compact");
  steps.type = "number";
  steps.value = loop.max_steps || 12;
  steps.title = "Hard ceiling on how many blocks run, whatever the arrows say";
  const field = (label, node, hint) => {
    const l = el("label", null, label);
    l.append(node);
    if (hint) l.append(el("span", "hint", hint));
    return l;
  };
  head.append(field("Name", name), field("Description", description),
              field("Max blocks", steps, "safety ceiling"));

  const prompt = el("textarea");
  prompt.rows = 2;
  prompt.value = loop.prompt || "";
  prompt.placeholder = "Given to every agent in this loop, on top of the step's own task.";

  const blocks = el("div", "loop-blocks");
  const redraw = () => {
    blocks.innerHTML = "";
    if (!loop.nodes.length) {
      blocks.append(el("p", "muted small",
        "Empty. Add the first agent — its outcomes become the slots you fill in."));
    }
    loop.nodes.forEach((node, index) => blocks.append(blockCard(loop, node, index, redraw)));

    const add = el("button", null, "+ agent");
    add.onclick = () => {
      const id = `n_${Math.random().toString(36).slice(2, 8)}`;
      // A new block exits successfully by default: the commonest wiring, and it
      // keeps the loop valid so it can be saved at any point.
      loop.nodes.push({ id, role: meta.agents[0] || "backend", focus: "", check: null,
                        on: { SUCCESS: { target: "exit", max_visits: 3 } } });
      if (!loop.start) loop.start = id;
      redraw();
    };
    const addRow = el("div", "row small");
    addRow.append(add);
    blocks.append(addRow);
  };
  redraw();

  const actions = el("div", "row small");
  const save = el("button", "primary", "Save");
  const result = el("span", "check-result");
  save.onclick = async () => {
    const body = {
      description: description.value.trim(),
      prompt: prompt.value,
      max_steps: Number(steps.value) || 12,
      start: loop.start,
      nodes: loop.nodes,
    };
    try {
      await api(`/api/loops/${encodeURIComponent(name.value.trim())}`,
                { method: "PUT", body });
    } catch (_) { return; }
    result.textContent = "saved";
    result.className = "check-result ok";
    toast(`Loop “${name.value.trim()}” saved.`);
    await renderLoops(name.value.trim());
  };
  actions.append(save, result);

  if (!isNew) {
    // Most loops are a variation on one that already works — a different
    // tester, one more block, a longer leash. Starting from the working one
    // beats retyping its wiring and getting an arrow wrong.
    const clone = el("button", null, "Clone");
    clone.title = "Copy this loop, wiring and all, under a new name";
    clone.onclick = () => {
      const copy = cloneLoop(loop);
      const box = $("loop-list");
      box.innerHTML = "";
      $("loop-summary").textContent = "unsaved copy — give it a name and save";
      box.append(loopCard(copy, true));
      toast(`Copy of “${loop.name}”. Save it to keep it.`);
    };
    actions.append(clone);

    const remove = el("button", "danger", "Delete");
    remove.onclick = async () => {
      const ok = await confirmDialog(`Delete the loop “${loop.name}”?`,
        "Steps using it must be changed first.");
      if (!ok) return;
      try {
        await api(`/api/loops/${encodeURIComponent(loop.name)}`, { method: "DELETE" });
      } catch (_) { return; }
      await renderLoops();
    };
    actions.append(remove);
  }

  card.append(head, el("label", "small", "Loop prompt — every agent in the loop sees this"),
              prompt, blocks, actions);
  return card;
}

function blockCard(loop, node, index, redraw) {
  const meta = state.loops || { agents: [], verifiers: [] };
  const wrap = el("div", "loop-block");
  const role = state.roles[node.role];
  if (role) wrap.style.borderLeftColor = role.color;

  const head = el("div", "row small");
  head.append(el("span", "flow-index", `${index + 1}.`));

  const who = el("select", "compact");
  meta.agents.forEach((a) => {
    const opt = el("option", null, a);
    opt.value = a;
    if (a === node.role) opt.selected = true;
    who.append(opt);
  });
  who.onchange = () => { node.role = who.value; redraw(); };
  head.append(who);

  const check = el("select", "compact");
  const noCheck = el("option", null, "no check");
  noCheck.value = "";
  check.append(noCheck);
  meta.verifiers.forEach((v) => {
    const opt = el("option", null, `checked by ${v}`);
    opt.value = v;
    if (v === node.check) opt.selected = true;
    check.append(opt);
  });
  check.onchange = () => {
    node.check = check.value || null;
    // CHECK FAILED can only happen when there is a check to fail.
    if (!node.check) delete node.on.CHECK_FAILED;
    redraw();
  };
  head.append(check);

  if (loop.start === node.id) {
    head.append(el("span", "badge", "starts here"));
  } else {
    const makeStart = el("button", "small", "start here");
    makeStart.onclick = () => { loop.start = node.id; redraw(); };
    head.append(makeStart);
  }

  const revert = el("input");
  revert.type = "checkbox";
  revert.checked = !!node.revert_on_fail;
  revert.onchange = () => { node.revert_on_fail = revert.checked; };
  const revertLabel = el("label", "row small");
  revertLabel.append(revert, document.createTextNode(" revert on failure"));
  revertLabel.title = "Undo this block's changes when it does not succeed, so a fixer "
                      + "that made things worse does not hand its mess to the next agent.";
  head.append(revertLabel);

  const remove = el("button", "small", "✕");
  remove.onclick = () => {
    loop.nodes = loop.nodes.filter((n) => n.id !== node.id);
    // Anything pointing at the removed block would dangle; fail out instead.
    loop.nodes.forEach((n) => Object.values(n.on || {}).forEach((routes) => {
      (Array.isArray(routes) ? routes : [routes]).forEach((edge) => {
        if (edge.target === node.id) edge.target = "fail";
      });
    }));
    if (loop.start === node.id) loop.start = loop.nodes[0] ? loop.nodes[0].id : "";
    redraw();
  };
  head.append(remove);

  const focus = el("textarea");
  focus.rows = 2;
  focus.value = node.focus || "";
  focus.placeholder = "This agent's part in the loop — 'run the tests, do not implement'";
  focus.oninput = () => { node.focus = focus.value; };

  const exits = el("div", "loop-exits");
  const outcomes = ["SUCCESS", "FAILED"].concat(node.check ? ["CHECK_FAILED"] : []);
  outcomes.forEach((outcome) => exits.append(exitRow(loop, node, outcome, redraw)));

  wrap.append(head, focus, exits);
  return wrap;
}

function exitRow(loop, node, outcome, redraw) {
  const row = el("div", "loop-exit");
  const head = el("div", "loop-exit-head");
  head.append(el("span", `exit-tag ${outcome.toLowerCase()}`, OUTCOME_LABEL[outcome]));

  // An outcome may take more than one arrow, in tiers: the first covers the
  // first N times it happens, the next covers the ones after that, and running
  // past the last one ends the loop.
  let routes = node.on[outcome];
  if (!routes) routes = [];
  else if (!Array.isArray(routes)) routes = [routes];
  node.on[outcome] = routes;

  const add = el("button", "small ghost", "+ then");
  add.title = "Add a tier: what to do once the arrows above have been used up";
  add.onclick = () => {
    routes.push({ target: routes.length ? routes[routes.length - 1].target : "fail",
                  max_visits: 2, backup: true });
    redraw();
  };
  head.append(add);
  row.append(head);

  if (!routes.length) routes.push({ target: "fail", max_visits: 3, backup: false });

  let taken = 0;
  routes.forEach((edge, index) => {
    const line = el("div", "loop-route");
    const leaves = ["exit", "fail"].includes(edge.target);
    if (routes.length > 1) {
      // Say which turns this arrow covers, since that is the whole point of
      // having more than one and it is not obvious from a count alone.
      const from = taken + 1;
      const label = leaves ? "then" : (edge.max_visits > 1
        ? `${ordinal(from)}–${ordinal(taken + edge.max_visits)}` : ordinal(from));
      line.append(el("span", "muted small route-when", label));
      if (!leaves) taken += Number(edge.max_visits) || 1;
    }

    const target = el("select", "compact");
    [["exit", "leave the loop — step succeeded"],
     ["fail", "leave the loop — step failed"]].forEach(([v, label]) => {
      const opt = el("option", null, label);
      opt.value = v;
      target.append(opt);
    });
    loop.nodes.forEach((n, i) => {
      const opt = el("option", null, `→ ${i + 1}. ${n.role}${n.id === node.id ? " (again)" : ""}`);
      opt.value = n.id;
      target.append(opt);
    });
    target.value = edge.target;

    const visits = el("input", "compact tiny");
    visits.type = "number";
    visits.min = "1";
    visits.value = edge.max_visits || 3;
    visits.title = routes.length > 1
      ? "How many times this arrow is taken before the next one takes over"
      : "How many times this arrow may be taken before the loop gives up";

    const backup = el("input");
    backup.type = "checkbox";
    backup.checked = !!edge.backup;
    const backupLabel = el("label", "row small");
    backupLabel.append(backup, document.createTextNode(" backup model"));
    backupLabel.title = "Run that agent on its backup model rather than its usual one. "
                        + "A retry changes the prompt; this is what changes the model.";

    const drop = el("button", "small", "✕");
    drop.title = "Remove this tier";
    drop.onclick = () => { routes.splice(index, 1); redraw(); };

    const sync = () => {
      const gone = ["exit", "fail"].includes(target.value);
      edge.target = target.value;
      edge.max_visits = Number(visits.value) || 3;
      edge.backup = backup.checked;
      // A count on "leave the loop" means nothing — it is taken once by
      // definition — and neither does a model for an agent that never runs.
      visits.hidden = gone;
      backupLabel.hidden = gone;
    };
    target.onchange = () => { sync(); redraw(); };
    visits.oninput = () => { sync(); if (routes.length > 1) redraw(); };
    backup.onchange = sync;
    sync();

    line.append(target, visits, backupLabel);
    if (routes.length > 1) line.append(drop);
    row.append(line);
  });

  if (routes.length > 1 && !["exit", "fail"].includes(routes[routes.length - 1].target)) {
    row.append(el("div", "muted small route-end",
                  `after ${taken}, the loop halts`));
  }
  return row;
}

function ordinal(n) {
  const tail = ["th", "st", "nd", "rd"][(n % 100 - 20) % 10] || ["th", "st", "nd", "rd"][n] || "th";
  return `${n}${tail}`;
}

$("add-loop").onclick = () => {
  // A new one takes over the modal too, so there is only ever one on screen.
  const box = $("loop-list");
  box.innerHTML = "";
  $("loop-summary").textContent = "unsaved";
  box.append(loopCard(
    { name: "", description: "", prompt: "", nodes: [], start: "", max_steps: 12 }, true));
};

/* ═════════════════════ files, and reviewing them ═══════════════════ */

/* Reading the code is how you find out whether the agents actually built what
 * you asked for, and the useful thing to do about a line that is wrong is to
 * say so on that line. A comment here is not a note to yourself: it becomes a
 * step the flow runs, and git says afterwards what was done about it. */

const fileState = { path: "", content: "", editing: false, files: [], totals: [] };

/* Nothing from the previous project belongs on screen. The file tree, the
 * statistics, the file that was open and the preview all name a directory this
 * session has never heard of — and a stale "24 files, 6,426 lines" is worse
 * than an empty panel, because it looks like an answer. */
function resetFiles() {
  Object.assign(fileState, { path: "", content: "", editing: false,
                             files: [], totals: [] });
  const tree = $("file-tree");
  if (tree) tree.innerHTML = "";
  const view = $("file-view");
  if (view) view.innerHTML = "";
  renderFileStats();
  renderPreviewStatus(null);
  if (view) renderFileView();
}

async function openFiles() {
  show("files");
  // Cleared before the fetch, not after it: the panel is on screen for the
  // length of a round trip, and what it shows until then must not be a lie.
  if (fileState.session !== (state.session || {}).id) resetFiles();
  fileState.session = (state.session || {}).id;
  await loadFileTree();
  renderReviewStatus();
  api(`/api/sessions/${state.session.id}/preview`)
    .then(renderPreviewStatus).catch(() => {});
  // Either way the pane gets drawn. Without the else it kept whatever markup
  // the page was served with, so the comment box only appeared once something
  // else had forced a re-render — opening a file and closing it again.
  if (fileState.path) await openFile(fileState.path);
  else renderFileView();
}

async function loadFileTree() {
  const data = await api(`/api/sessions/${state.session.id}/files`);
  fileState.files = data.files || [];
  fileState.totals = data.totals || [];
  renderFileTree();
  renderFileStats();
}

/* What is actually in a project is a question about kinds of file. "4,300
 * lines" says almost nothing; 4,000 of JavaScript and 300 of CSS says what was
 * built. */
function renderFileStats() {
  const box = $("file-stats-body");
  if (!box) return;
  box.innerHTML = "";
  const totals = fileState.totals || [];
  if (!totals.length) {
    box.append(el("p", "muted small", "Nothing here yet."));
    return;
  }
  const lines = totals.reduce((sum, t) => sum + t.lines, 0);
  const files = totals.reduce((sum, t) => sum + t.files, 0);
  const summary = $("file-stats").querySelector("summary");
  if (summary) {
    summary.textContent = `${lines.toLocaleString()} lines in ${files} files`;
  }

  const most = Math.max(...totals.map((t) => t.lines), 1);
  totals.forEach((t) => {
    const row = el("div", "stat-row-file");
    row.append(el("span", "stat-ext", `.${t.ext}`.replace("..", ".")));
    const bar = el("span", "stat-bar");
    const fill = el("span", "stat-fill");
    fill.style.width = `${Math.max(2, Math.round((t.lines / most) * 100))}%`;
    bar.append(fill);
    row.append(bar);
    row.append(el("span", "stat-lines", t.lines.toLocaleString()));
    row.append(el("span", "muted small", `${t.files}f`));
    row.title = `${t.files} file(s), ${t.lines.toLocaleString()} lines, `
                + `${Math.ceil(t.bytes / 1024).toLocaleString()}k`;
    box.append(row);
  });
}

function renderFileTree() {
  const box = $("file-tree");
  box.innerHTML = "";
  const filter = $("file-filter").value.trim().toLowerCase();
  const shown = fileState.files.filter((f) => !filter || f.path.toLowerCase().includes(filter));
  if (!shown.length) {
    box.append(el("p", "muted small", filter ? "Nothing matches." : "No files yet."));
    return;
  }

  // Grouped by directory: a flat list of 200 paths is not a tree, and the
  // browser can build the shape without the server pretending to.
  const dirs = new Map();
  shown.forEach((file) => {
    const cut = file.path.lastIndexOf("/");
    const dir = cut === -1 ? "" : file.path.slice(0, cut);
    if (!dirs.has(dir)) dirs.set(dir, []);
    dirs.get(dir).push(file);
  });

  [...dirs.keys()].sort().forEach((dir) => {
    const group = el("details", "file-group");
    group.open = true;
    group.append(el("summary", "muted small", dir || "/"));
    dirs.get(dir).forEach((file) => {
      const row = el("div", "file-row");
      if (file.path === fileState.path) row.classList.add("open");
      const marks = (state.session.review || []).filter((n) => n.path === file.path).length;
      row.append(el("span", "file-name", file.path.split("/").pop()));
      if (marks) row.append(el("span", "badge review-count", String(marks)));
      // A page is worth opening, not just reading: reading index.html tells you
      // it exists, and running it tells you whether the thing works.
      if (/\.(html?|svg)$/i.test(file.path)) {
        const open = el("button", "file-open", "▷");
        open.title = "Serve this folder as files and open the page in a new tab. "
                     + "Nothing is built or run — a project with a build step is "
                     + "yours to start.";
        open.onclick = (e) => { e.stopPropagation(); openPreview(file.path); };
        row.append(open);
      }
      row.onclick = () => openFile(file.path);
      group.append(row);
    });
    box.append(group);
  });
}

async function openFile(path) {
  // Clicking the file that is already open closes it, which is also how you get
  // back to the general comment — the same click, in the same place.
  if (path === fileState.path) return closeFile();

  let data;
  try {
    data = await api(`/api/sessions/${state.session.id}/file?path=${encodeURIComponent(path)}`);
  } catch (_) { return; }
  fileState.path = path;
  fileState.content = data.content;
  fileState.editing = false;
  $("file-name").textContent = path;
  $("file-meta").textContent = `${data.lines} lines · ${Math.ceil(data.bytes / 1024)}k`
    + " · click a line number to comment on that line";
  renderFileTree();
  renderFileView();
}

function closeFile() {
  fileState.path = "";
  fileState.content = "";
  fileState.editing = false;
  renderFileTree();
  renderFileView();
}

const CM_MODES = {
  js: "javascript", jsx: "javascript", mjs: "javascript", cjs: "javascript",
  ts: {name: "javascript", typescript: true}, tsx: {name: "javascript", typescript: true},
  json: {name: "javascript", json: true},
  py: "python", css: "css", html: "htmlmixed", htm: "htmlmixed",
  xml: "xml", svg: "xml", md: "markdown", markdown: "markdown",
};

function modeFor(path) {
  return CM_MODES[(path.split(".").pop() || "").toLowerCase()] || null;
}

/* CodeMirror does the editor — highlighting, selection, brackets, undo — and
 * its gutter does the reviewing. Clicking a line number is the whole gesture:
 * a comment appears under that line as a widget, and stays attached to it. */
let editor = null;
const lineWidgets = [];

function fileEditing(on) {
  fileState.editing = on;
  $("file-edit").hidden = on;
  $("file-save").hidden = !on;
  $("file-cancel").hidden = !on;
  if (editor) {
    editor.setOption("readOnly", !on);
    $("file-view").classList.toggle("editing", on);
    if (on) editor.focus();
  }
}

function renderFileView() {
  const box = $("file-view");
  if (!fileState.path) {
    if (editor) { editor.toDom = null; }
    return renderGeneralComment(box);
  }

  // No CodeMirror (the test harness, or a browser that failed to load it):
  // plain numbered lines, still clickable, still reviewable.
  if (typeof CodeMirror === "undefined") return renderPlainFileView();

  box.innerHTML = "";
  editor = CodeMirror(box, {
    value: fileState.content,
    mode: modeFor(fileState.path),
    theme: "material-darker",
    lineNumbers: true,
    lineWrapping: false,
    readOnly: true,
    styleActiveLine: true,
    matchBrackets: true,
    gutters: ["CodeMirror-linenumbers", "review-gutter"],
  });
  // The line number is the button. Clicking the line itself has to stay free
  // for selecting and copying, which is most of what reading code is.
  editor.on("gutterClick", (cm, line) => {
    if (fileState.editing) return;
    commentOn(line + 1, cm.getLine(line) || "");
  });
  fileEditing(false);
  paintReviewMarks();
}

function paintReviewMarks() {
  if (!editor) return;
  lineWidgets.splice(0).forEach((w) => w.clear());
  editor.clearGutter("review-gutter");

  (state.session.review || [])
    .filter((n) => n.path === fileState.path)
    .forEach((note) => {
      const line = Math.min(note.line, editor.lineCount()) - 1;
      editor.setGutterMarker(line, "review-gutter", el("span", "review-dot", "●"));

      const card = el("div", "code-note");
      card.append(el("span", "badge", "you"), el("span", null, note.note));
      const drop = el("button", "small", "✕");
      drop.title = "Take this comment back";
      drop.onclick = async () => {
        await api(`/api/sessions/${state.session.id}/review/${note.id}`, { method: "DELETE" });
        state.session.review = state.session.review.filter((x) => x.id !== note.id);
        paintReviewMarks();
        renderReviewStatus();
        renderSessionBar();
        renderFileTree();
      };
      card.append(drop);
      lineWidgets.push(editor.addLineWidget(line, card, { coverGutter: false }));
    });
}

function renderPlainFileView() {
  const box = $("file-view");
  box.innerHTML = "";
  const notes = (state.session.review || []).filter((n) => n.path === fileState.path);
  fileState.content.split("\n").forEach((text, i) => {
    const n = i + 1;
    const row = el("div", "code-line");
    row.append(el("span", "code-n", String(n)), el("code", "code-text", text || " "));
    const gutter = row.firstChild;
    gutter.title = "Comment on this line";
    gutter.onclick = () => commentOn(n, text);
    box.append(row);
    notes.filter((note) => note.line === n).forEach((note) => {
      const card = el("div", "code-note");
      card.append(el("span", "badge", "you"), el("span", null, note.note));
      box.append(card);
    });
  });
}

//: The comment box being written, so a second click closes it rather than
//: stacking another one under a different line.
let composing = null;

function commentOn(line, code) {
  if (fileState.editing) return;
  // Clicking again — the same line or another — puts the open one away first.
  const wasOn = composing && composing.line;
  if (composing) {
    composing.close();
    composing = null;
    if (wasOn === line) return;              // a second click on the same line
  }

  const form = el("div", "code-compose");
  form.append(el("span", "muted small", `line ${line}`));
  const input = el("input");
  input.placeholder = "What is wrong here, or what should it do instead?";
  const send = el("button", "primary", "Comment");
  const cancel = el("button", null, "Cancel");

  let widget = null;
  const close = () => {
    if (widget) widget.clear(); else form.remove();
    if (composing && composing.form === form) composing = null;
  };
  cancel.onclick = (e) => { e.stopPropagation(); close(); };
  send.onclick = async (e) => {
    e.stopPropagation();
    const note = input.value.trim();
    if (!note) return;
    const saved = await api(`/api/sessions/${state.session.id}/review`, {
      method: "POST", body: { path: fileState.path, line, code, note },
    });
    state.session.review = [...(state.session.review || []), saved];
    close();
    if (editor) paintReviewMarks(); else renderFileView();
    renderReviewStatus();
    renderSessionBar();
    renderFileTree();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") send.onclick(e);
    if (e.key === "Escape") { e.stopPropagation(); close(); }
  });
  form.append(input, send, cancel);

  if (editor) {
    widget = editor.addLineWidget(line - 1, form, { coverGutter: false });
  } else {
    const rows = $("file-view").querySelectorAll(".code-line");
    const anchor = rows[line - 1];
    if (anchor && anchor.after) anchor.after(form); else $("file-view").append(form);
  }
  composing = { line, form, close };
  input.focus();
}

function renderReviewStatus() {
  const box = $("files-status");
  box.innerHTML = "";
  const pending = (state.session?.review || []).length;
  box.append(el("b", null, "Review"));
  box.append(el("span", "muted small", pending
    ? `${pending} comment(s) waiting — “Review finished” sends them as a step`
    : "Click a line number to comment on it, or say something about the whole thing."));
  $("review-finish").disabled = !pending;

}

/* Not everything worth saying is about a line. "The controls are unusable on a
 * phone" is about the result, and making it fit a line number means picking one
 * arbitrarily — or not writing it down at all.
 *
 * So it goes where the editor goes: no file selected means this pane is for
 * comments about the whole thing, at the same size and in the same place as the
 * code it is about. */
function renderGeneralComment(box) {
  // The header names what the pane is for, and it is not a file right now.
  $("file-name").textContent = "Review";
  $("file-meta").textContent = "about the whole thing — or pick a file to comment on lines";
  box.innerHTML = "";
  const wrap = el("div", "general-review");
  wrap.append(el("h3", null, "A comment about the whole thing"));
  wrap.append(el("p", "muted small",
    "No file needed. Pick a file on the left to comment on particular lines — "
    + "click it again to come back here."));

  const text = el("textarea", "general-note");
  text.rows = 4;
  text.placeholder = "The controls are unusable on a phone.";
  const add = el("button", "primary", "Add comment");
  const send = async () => {
    const note = text.value.trim();
    if (!note || !state.session) return;
    add.disabled = true;
    try {
      const entry = await api(`/api/sessions/${state.session.id}/review`,
                              { method: "POST", body: { note } });
      state.session.review = (state.session.review || []).concat([entry]);
      text.value = "";
      renderReviewStatus();
      renderGeneralComment(box);
    } finally { add.disabled = false; }
  };
  add.onclick = send;
  // Enter for a newline, Ctrl/Cmd+Enter to add: this is a paragraph, not a
  // search box, and a stray Enter should not send it half-written.
  text.onkeydown = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) send(); };
  wrap.append(text, add);

  const waiting = state.session?.review || [];
  if (waiting.length) {
    wrap.append(el("h3", null, `Waiting to be sent (${waiting.length})`));
    waiting.forEach((note) => {
      const row = el("div", "review-note");
      row.append(el("span", "muted small", note.path
        ? `${note.path}:${note.line}` : "overall"));
      row.append(el("span", null, note.note));
      const drop = el("button", "small", "✕");
      drop.title = "Remove this comment";
      drop.onclick = async () => {
        await api(`/api/sessions/${state.session.id}/review/${note.id}`, { method: "DELETE" });
        state.session.review = state.session.review.filter((n) => n.id !== note.id);
        renderReviewStatus();
        renderGeneralComment(box);
      };
      row.append(drop);
      wrap.append(row);
    });
  }
  box.append(wrap);
}

async function openPreview(path) {
  let served;
  try {
    served = await api(`/api/sessions/${state.session.id}/preview`,
                       { method: "POST", body: { path } });
  } catch (_) { return; }

  // Opening a page that cannot possibly work, and leaving the reason in a
  // toast that has already gone, is how you end up debugging the wrong thing.
  if (served.needs_build) {
    warnAboutBuild(served);
    renderPreviewStatus(served);
    return;
  }
  showPage(served);
}

function showPage(served) {
  // A new tab rather than an iframe: the page gets its own origin, its own
  // console and its own devtools, which is what you need to judge it.
  window.open(served.open, "_blank", "noopener");
  toast(served.public
    ? `Serving ${served.root.split("/").pop()}/ — share ${served.public}`
    : `Serving ${served.root.split("/").pop()}/ — on this network at ${served.url}`);
  renderPreviewStatus(served);
}

/* ngrok is a program on your machine, not a service trance talks to, so this
 * starts it and waits for it to say where it landed. Anything it complains
 * about — no authtoken, no account — is shown as it said it. */
async function startShare(button) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "starting…";
  try {
    const tunnel = await api(`/api/sessions/${state.session.id}/share`,
                             { method: "POST", body: { protected: false } });
    state.shared = tunnel;
    if (state.preview) state.preview.public = tunnel.url;
    renderPreviewStatus();
    toast(tunnel.adopted
      ? `Using the ngrok tunnel you already had: ${tunnel.url}`
      : `Anyone with this link can open it: ${tunnel.url}`);
  } catch (_) {
    button.disabled = false;
    button.textContent = label;
  }
}

function warnAboutBuild(served) {
  const box = $("preview-warning-body");
  box.innerHTML = "";
  const first = (served.blocked_by || [])[0];
  box.append(el("p", null,
    "trance serves this folder as files. It does not build or run anything — "
    + "that stays yours to start."));
  if (first) {
    box.append(el("p", "muted small",
      `${first.file}:${first.line} imports '${first.specifier}', which is a package `
      + `name rather than a path. Only a bundler or an import map can say which `
      + `file that is, so the page will load and then stop at this import.`));
  }
  if (served.build_command) {
    const cmd = el("pre", "cmd", `cd ${served.root}\n${served.build_command}`);
    cmd.title = "Click to copy";
    cmd.onclick = () => {
      if (navigator.clipboard) navigator.clipboard.writeText(served.build_command);
      toast("Copied.");
    };
    box.append(cmd);
  }
  box.append(el("p", "muted small",
    "Opening it anyway is still useful for checking markup and CSS."));

  $("preview-anyway").onclick = () => {
    $("preview-warning").classList.remove("open");
    showPage(served);
  };
  $("preview-warning").classList.add("open");
}

function renderPreviewStatus(served) {
  // Its own element. It used to share one with the review status, which clears
  // what is there before rendering — so writing a comment took the share link
  // off the screen, and the only way back was to serve the page again.
  if (served !== undefined) state.preview = served;
  const box = $("preview-status");
  if (!box) return;
  box.innerHTML = "";
  served = state.preview;
  if (!served || !served.url) return;

  const note = el("span", "preview-note");
  const link = el("a", null, served.url.replace(/^https?:\/\//, ""));
  // The network address, deliberately: the loopback one is already open in the
  // tab this came from, and this is the one worth typing into a phone.
  link.href = served.url;
  link.target = "_blank";
  link.rel = "noopener";
  link.title = "This preview, as reachable from any device on your network";
  note.append(el("span", "muted small", "serving"), link);

  // Sharing is a click, but never an accident: the button says what it will do
  // and the link is only public once you have pressed it.
  if (!served.public) {
    const share = el("button", "small", "share…");
    share.title = "Publish this preview over HTTPS with ngrok, so you can send "
                  + "someone the link";
    share.onclick = () => startShare(share);
    note.append(share);
  }
  if (served.public) {
    const share = el("a", "share-link", "share");
    share.href = served.public;
    share.target = "_blank";
    share.rel = "noopener";
    share.title = `${served.public} — click to copy`;
    share.onclick = (e) => {
      e.preventDefault();
      if (navigator.clipboard) navigator.clipboard.writeText(served.public);
      toast(`Copied ${served.public}`);
    };
    note.append(share);
  }
  // Only for a tunnel trance started. One it merely found belongs to whoever
  // ran ngrok, and a button that silently does nothing is worse than no button.
  if (state.shared && state.shared.running && !state.shared.adopted) {
    const unshare = el("button", "small", "stop sharing");
    unshare.title = "Close the public link. The preview stays up locally.";
    unshare.onclick = async () => {
      await api(`/api/sessions/${state.session.id}/share`, { method: "DELETE" });
      state.shared = null;
      if (state.preview) state.preview.public = "";
      renderPreviewStatus();
      toast("The public link is closed.");
    };
    note.append(unshare);
  }

  const stop = el("button", "small", "stop");
  stop.onclick = async () => {
    await api(`/api/sessions/${state.session.id}/preview`, { method: "DELETE" });
    note.remove();
    toast("Preview stopped.");
  };
  note.append(stop);
  box.append(note);
}

$("file-filter").addEventListener("input", renderFileTree);

$("file-edit").onclick = () => fileEditing(true);
$("file-cancel").onclick = () => renderFileView();
$("file-save").onclick = async () => {
  const content = editor ? editor.getValue() : fileState.content;
  await api(`/api/sessions/${state.session.id}/file`, {
    method: "PUT", body: { path: fileState.path, content },
  });
  fileState.content = content;
  renderFileView();
  toast("Saved, and committed so it is in the history.");
};

$("review-finish").onclick = async () => {
  const result = await api(`/api/sessions/${state.session.id}/review/finish`,
                           { method: "POST" });
  state.session.review = [];
  state.session.flow = result.flow;
  renderReviewStatus();
  renderFileView();
  renderSessionBar();
  toast(result.started
    ? `Sent ${result.notes.length} comment(s) — the flow is working on them.`
    : `Sent ${result.notes.length} comment(s) as a step. Press Run to start it.`);
};

$("review-changes").onclick = () => showReviewHistory();

/* Every review you have sent, newest first. The latest is open because it is
 * the one you are waiting on; the rest are folded, because history is worth
 * having and not worth scrolling past. */
async function showReviewHistory() {
  const body = await api(`/api/sessions/${state.session.id}/reviews`);
  const box = $("file-view");
  fileEditing(false);
  box.innerHTML = "";
  $("file-name").textContent = "Review history";

  const reviews = body.reviews || [];
  $("file-meta").textContent = reviews.length
    ? `${reviews.length} review(s) sent` : "";
  if (!reviews.length) {
    box.append(el("p", "muted small", "No review has been sent yet."));
    return;
  }
  reviews.forEach((review, index) => box.append(reviewSection(review, index === 0)));
}

function reviewSection(review, open) {
  const section = el("details", "review-section");
  section.open = open;

  const summary = el("summary");
  summary.append(el("span", `badge ${review.status}`, review.status));
  summary.append(el("span", "review-when", when(review.at)));
  summary.append(el("span", "muted small",
    `${review.notes.length} comment(s) · ${(review.commits || []).length} commit(s)`
    + ` · ${review.files.length} file(s)`));
  section.append(summary);

  const inner = el("div", "review-body");
  review.notes.forEach((note) => {
    const row = el("div", "code-note");
    row.append(el("span", "badge", note.path ? `${note.path}:${note.line}` : "overall"),
               el("span", null, note.note));
    inner.append(row);
  });

  const commits = review.commits || [];
  if (commits.length) {
    commits.forEach((commit) => inner.append(commitRow(commit)));
  } else {
    inner.append(el("p", "muted small",
      review.status === "done" || review.status === "failed"
        ? "That step finished without changing any files."
        : "Nothing yet — this fills in as the step runs."));
  }
  section.append(inner);
  return section;
}

//: "2026-08-07T13:09:53+00:00" -> something you can read at a glance.
function when(stamp) {
  if (!stamp) return "";
  const at = new Date(stamp);
  if (isNaN(at)) return "";
  const today = new Date().toDateString() === at.toDateString();
  return today ? at.toLocaleTimeString() : at.toLocaleString();
}

/* One commit, folded. Opening it fetches its patch — a run can be dozens of
 * commits and nobody wants all of their diffs at once. */
function commitRow(commit) {
  const row = el("details", "commit-row");
  const summary = el("summary");
  summary.append(el("span", "badge mono", commit.short),
                 el("span", "commit-subject", commit.subject || "(no message)"));
  if (commit.files) {
    summary.append(el("span", "muted small",
      `${commit.files} file(s)`),
      el("span", "diff-added", `+${commit.added}`),
      el("span", "diff-removed", `−${commit.removed}`));
  }
  summary.append(el("span", "muted small", commit.when || ""));
  row.append(summary);

  const body = el("div", "commit-body");
  body.append(el("p", "muted small", "Loading…"));
  row.append(body);

  let loaded = false;
  row.addEventListener("toggle", async () => {
    if (loaded || !row.open) return;
    loaded = true;
    try {
      const full = await api(`/api/sessions/${state.session.id}/commit/${commit.sha}`);
      body.innerHTML = "";
      if (full.stat) body.append(el("pre", "commit-stat", full.stat));
      body.append(renderDiff(full.diff));
      if (full.clipped) {
        body.append(el("p", "muted small",
          "…this commit is too large to show in full — `git show " + commit.short
          + "` has the rest."));
      }
    } catch (_) {
      loaded = false;
      body.innerHTML = "";
      body.append(el("p", "muted small err", "Could not load this commit."));
    }
  });
  return row;
}
