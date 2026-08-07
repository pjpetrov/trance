// Loads app.js in a DOM-less harness and calls the entry points, so a call to
// a function that no longer exists fails here instead of in the browser.
const fs = require("fs");
const src = fs.readFileSync("src/trance/server/static/app.js", "utf8");

const nodes = new Map();
const makeEl = (tag = "div") => {
  const el = {
    tagName: tag.toUpperCase(), className: "", style: {}, dataset: {}, children: [],
    textContent: "", innerHTML: "", value: "", checked: false, disabled: false,
    title: "", placeholder: "", type: "",
    append: (...c) => el.children.push(...c), prepend: () => {}, remove: () => {},
    appendChild: (c) => el.children.push(c),
    addEventListener: () => {}, removeEventListener: () => {},
    // Null, like a real one that finds nothing. Returning an element made every
    // "is this here?" check pass, which is how a missing .c-head went unseen.
    querySelector: (sel) => el._found[sel] || null,
    querySelectorAll: () => [],
    // A real set, not a stub: code that branches on contains() (which screen is
    // active, is a modal open) took the same path every time otherwise.
    classList: (() => {
      const set = new Set();
      return { add: (...c) => c.forEach((x) => set.add(x)),
               remove: (...c) => c.forEach((x) => set.delete(x)),
               toggle: (c, on) => (on ?? !set.has(c)) ? set.add(c) : set.delete(c),
               contains: (c) => set.has(c) };
    })(),
    focus: () => {}, select: () => {}, setAttribute: () => {},
    scrollTop: 0, scrollHeight: 0, clientHeight: 0, _found: {},
    get firstChild() { return el.children[0] || makeEl(); },
  };
  return el;
};
const openModals = [];
const listeners = {};
global.document = {
  addEventListener: (type, fn) => { listeners[type] = fn; },
  activeElement: null,
  getElementById: (id) => { if (!nodes.has(id)) nodes.set(id, makeEl()); return nodes.get(id); },
  createElement: makeEl, createTextNode: (t) => ({ text: t }),
  createElementNS: (_ns, tag) => makeEl(tag),
  querySelectorAll: (sel) => (sel === ".modal.open" ? openModals : []),
  body: makeEl(),
};
global.window = { isSecureContext: false };
global.navigator = {};
global.location = { protocol: "http:", host: "x" };
global.WebSocket = function () { return { onmessage: null, onclose: null, close() {} }; };
const RESPONSES = {
  "/api/config": { roles: {}, providers: [], presets: [], kinds: {},
                   planning: { max_step_points: 5, scale: [1, 2, 3, 5, 8, 13] },
                   orchestrator: { provider: "p", model: "m", base_url: "u" } },
  "/api/workspace": { workspace: "/w", writable: true, suggested_name: "project",
                      suggested_dir: "/w/project", state_dir: "/s" },
  "/api/sessions": [],
  "/api/loops": { loops: [{ name: "another-loop", description: "d2", prompt: "",
                            start: "n_a", max_steps: 6, roles: ["reviewer"],
                            nodes: [{ id: "n_a", role: "reviewer", focus: "",
                                      check: null, revert_on_fail: false,
                                      on: { SUCCESS: { target: "exit", max_visits: 1 } } }] },
                          { name: "test-and-fix", description: "d", prompt: "p",
                            start: "n_test", max_steps: 10, roles: ["tester", "backend"],
                            nodes: [{ id: "n_test", role: "tester", focus: "run tests",
                                      check: null,
                                      on: { SUCCESS: { target: "exit", max_visits: 3 },
                                            FAILED: { target: "n_fix", max_visits: 3 } } },
                                    { id: "n_fix", role: "backend", focus: "fix it",
                                      check: "factchecker",
                                      on: { SUCCESS: { target: "n_test", max_visits: 3 },
                                            CHECK_FAILED: { target: "fail", max_visits: 1 } } }] }],
                  outcomes: ["SUCCESS", "FAILED", "CHECK_FAILED"], stops: ["exit", "fail"],
                  agents: ["tester", "backend"], verifiers: ["factchecker"] },
  "/api/commands": { allowed: ["ls"], shell: true, names: ["default"], default: "default",
                     lists: { default: { allowed: ["ls"], shell: true } }, defaults: ["ls"],
                     usage: {}, overrides: {}, agents_with_commands: [] },
  "/api/sessions/s1/memory": { path: "/p/.trance/memory.md", raw: "- **backend**: port 3100",
                               notes: ["- **backend**: port 3100", "- plain note"],
                               prompt_view: "- **backend**: port 3100",
                               oversized: false, max_notes: 25 },
};
global.fetch = async (path) => ({
  ok: true, status: 200,
  json: async () => RESPONSES[path] ?? {},
});

const module_ = { exports: {} };
new Function("module", "exports", src + "\n;module.exports={state,openSession,renderRun,renderFlowView,renderChat,renderFlowEditor,stepCard,consoleAppend,trackActivity,consoleReset,paintPaused,renderSessionBar,renderFlowEditor,redrawEditor,openStep,groupStepEvents,contextGauge,renderMemory,openMemory,loadMemory,paintMemoryCount,renderStepSize,pointsBadge,applyRefinedFlow,draftFingerprint,loopCard,renderLoops};")(module_, module_.exports);
const api = module_.exports;

// Drive the paths a user takes when opening a session.
const session = {
  id: "s1", name: "p", project_dir: "/tmp/p", status: "ready", paused: false,
  chat: [{ role: "user", content: "hi" }],
  team: [{ name: "backend", color: "#7aa2f7", description: "d", paths: [], toolsets: [] }],
  flow: { steps: [{ id: "st1", role: "backend", task: "t", status: "done", checker: "tester",
                    fixer: "backend", loop_limit: 2, attempts: [], checks: ["tester"] }] },
  progress: { total: 1, done: 1 },
};
global.state = undefined;
const flat = (n) => !n ? "" : (n.textContent || "") + (n.children || []).map(flat).join(" ");
(async () => {
try {
  // state lives inside the module scope; exercise the renderers that read it
  // Render a real session with steps in every status — an empty flow used to
  // skip stepCard entirely, which is how a ReferenceError in it went unnoticed.
  api.state.session = session;
  api.state.roles = { backend: { name: "backend", color: "#7aa2f7", verifier: false },
                      tester: { name: "tester", color: "#f7768e", verifier: true } };
  api.state.planning = { max_step_points: 5, scale: [1, 2, 3, 5, 8, 13] };
  // One step mid-split: the marker belongs on that card, not above the plan.
  api.state.splitting = { count: 1, threshold: 5, step_ids: ["s4"] };
  api.state.draftSteps = ["pending", "running", "done", "failed", "skipped", "blocked"]
    .map((status, i) => ({ id: `s${i}`, role: "backend", task: `task ${i}`, status,
                           check: "tester", on_fail: null, max_loops: 2, checker: "tester",
                           fixer: "backend", loop_limit: 2, attempts: [],
                           points: [0, 1, 3, 5, 8, 13][i] }));
  api.state.draftSteps.forEach((step, i) => api.stepCard(step, i));   // every status
  const splitting = api.stepCard(api.state.draftSteps[4], 4);
  if (!flat(splitting).includes("splitting…")) {
    console.log("BROKEN: the step being split is not marked");
    process.exit(1);
  }
  if (flat(api.stepCard(api.state.draftSteps[0], 0)).includes("splitting…")) {
    console.log("BROKEN: a step that is not being split is marked");
    process.exit(1);
  }
  api.state.splitting = null;

  api.renderSessionBar(); api.renderChat(); api.renderFlowEditor();
  api.renderFlowView(); api.renderRun(); api.paintPaused();
  api.consoleReset();
  api.consoleAppend({ type: "step_started", agent: "backend", step_id: "st1",
                      ts: new Date().toISOString(), payload: { task: "t", attempt: 1 } });
  // A loop step rescopes the console; without it the loop's events were all
  // dropped because they belong to a different step than the last step_started.
  api.consoleAppend({ type: "loop_node", agent: "tester", step_id: "st9",
                      ts: new Date().toISOString(),
                      payload: { loop: "test-and-fix", visit: 1, role: "tester",
                                 message: "test-and-fix: tester" } });
  api.consoleAppend({ type: "tool_call", agent: "tester", step_id: "st9",
                      ts: new Date().toISOString(),
                      payload: { name: "run_command", ok: true, arguments: {}, result: "",
                                 detail: { kind: "command", command: "npm test",
                                           exit_code: 0, seconds: 1, output: "ok" } } });
  // And a permission prompt for some other step still has to be answerable.
  api.consoleAppend({ type: "approval_requested", agent: "backend", step_id: "elsewhere",
                      ts: new Date().toISOString(),
                      payload: { id: "ap_9", kind: "command", agent: "backend",
                                 subject: "npx jest", detail: { programs: ["npx"] },
                                 timeout_s: 300, message: "backend wants to run npx" } });
  {
    const shown = flat(document.getElementById("console")).replace(/\s+/g, " ");
    if (!shown.includes("npm test")) {
      console.log("BROKEN: a loop block's events were dropped by the console scope");
      process.exit(1);
    }
    if (!shown.includes("wants to run npx")) {
      console.log("BROKEN: a permission prompt was filtered out by the console scope");
      process.exit(1);
    }
  }
  api.consoleAppend({ type: "tool_call", agent: "backend", step_id: "st1",
                      ts: new Date().toISOString(),
                      payload: { name: "write_file", ok: true, arguments: {}, result: "",
                                 detail: { kind: "write", path: "a.py", created: true,
                                           added: 2, removed: 0, diff: "+x" } } });
  // Every console detail kind, so a branch that references an undeclared name
  // is caught here rather than mid-run.
  for (const detail of [{ kind: "command", command: "ls", exit_code: 0, seconds: 1, output: "" },
                        { kind: "command", command: "ls", exit_code: 1, seconds: 1, output: "" },
                        { kind: "background", command: "node s.js" },
                        { kind: "command_stopped", command_id: "c1" },
                        { kind: "truncated", limit: 4096, attempt: 1 },
                        { kind: "read", path: "tests/integration.test.js", deduped: true },
                        { kind: "read", path: "a.js", bytes: 20, start_line: 1, last_line: 3,
                          lines: 3 },
                        { kind: "read", path: "chart.js", outline: true, symbols: 6, lines: 332 },
                        { kind: "read", path: "big.js", start_line: 1, last_line: 666,
                          lines: 1818 },
                        { kind: "graph", tool: "search_symbols", hit: false,
                          query: "SSE done event equity curve" },
                        { kind: "graph", tool: "get_definition", hit: true,
                          query: "streamBacktest" },
                        { kind: "memory", note: "port 3100", stored: true, agent: "backend" },
                        { kind: "memory", note: "port 3100", stored: false, agent: "frontend" },
                        { kind: "read", path: "a.py" },
                        { kind: "refused_program", program: "npx", command: "npx vite" },
                        null]) {
    api.consoleAppend({ type: "tool_call", agent: "backend", step_id: "st1",
                        ts: new Date().toISOString(),
                        payload: { name: "run_command", ok: !detail || detail.kind !== "truncated",
                                   arguments: {}, result: "out", detail } });
  }
  api.trackActivity({ type: "step_started", agent: "backend", step_id: "st1",
                      ts: new Date().toISOString(), payload: { task: "t", attempt: 1 } });
  // The context gauge, at every pressure band plus the no-usage fallback.
  for (const ctx of [{ tokens: 900, window: 64000, budget: 59000, reserved: 4096,
                       percent: 1.4, estimated: false },
                     { tokens: 48000, window: 64000, budget: 59000, reserved: 4096,
                       percent: 75, estimated: false },
                     { tokens: 61000, window: 64000, budget: 59000, reserved: 4096,
                       percent: 95.3, estimated: true },
                     null]) {
    api.trackActivity({ type: "model_call", agent: "backend", ts: new Date().toISOString(),
                        payload: { model: "m", tool_calls: [], messages: [], summary: {},
                                   context: ctx } });
  }
  for (const ev of [
    { type: "fixing", agent: "backend", step_id: "st1",
      payload: { message: "Backend will fix it", loop: 1, of: 2,
                 handoff: "$ npm test → exit 1", handoff_chars: 240 } },
    { type: "fixed", agent: "backend", step_id: "st1",
      payload: { summary: "inverted the collision", files: ["server/game.js"] } },
    { type: "step_retry", agent: "tester", step_id: "st1",
      payload: { message: "tester will try again", feedback: "bug" } },
    { type: "memory_compacted", agent: "orchestrator",
      payload: { compacted: true, before: 30, after: 4, archive: "/p/.trance/memory.archive.md",
                 notes: ["- **backend**: port 3100"] } },
    { type: "memory_compacted", agent: "orchestrator",
      payload: { compacted: false, reason: "the rewrite produced no notes" } },
    { type: "splitting_steps", agent: "orchestrator",
      payload: { count: 1, threshold: 5, step_ids: ["s4"], tasks: ["build it all"],
                 message: "1 step(s) are over 5 points — breaking them up." } },
    { type: "approval_requested", agent: "tester", step_id: "st1",
      payload: { id: "ap_1", kind: "write", agent: "tester", subject: "jest.config.js",
                 detail: { remit: ["tests/**"] }, timeout_s: 300,
                 message: "tester wants to write jest.config.js" } },
    { type: "approval_requested", agent: "tester", step_id: "st1",
      payload: { id: "ap_2", kind: "command", agent: "tester", subject: "npx jest",
                 detail: { programs: ["npx"] }, timeout_s: 300,
                 message: "tester wants to run npx" } },
    { type: "approval_resolved", agent: "tester", step_id: "st1",
      payload: { id: "ap_1", kind: "write", subject: "jest.config.js", decision: "always",
                 detail: {} } },
    { type: "approval_resolved", agent: "tester", step_id: "st1",
      payload: { id: "ap_2", kind: "command", subject: "npx jest", decision: "deny",
                 detail: { timed_out: true } } },
  ]) {
    api.consoleAppend({ ...ev, ts: new Date().toISOString() });
    api.trackActivity(ev);
  }
  api.trackActivity({ type: "context_trimmed", agent: "backend",
                      ts: new Date().toISOString(),
                      payload: { dropped_tool_results: 2, budget: 59000,
                                 context_window: 64000 } });
  // The gauge is the one widget whose *text* matters, so assert it rather
  // than only checking that it rendered.
  const gauge = api.contextGauge({ tokens: 48000, window: 64000, budget: 59000,
                                   reserved: 4096, percent: 75, estimated: false });
  const text = flat(gauge).replace(/\s+/g, " ").trim();
  if (!text.includes("75%") || !text.includes("48k / 64k")) {
    console.log("BROKEN: gauge text is", JSON.stringify(text));
    process.exit(1);
  }
  if (gauge.className !== "ctx-gauge warm") {
    console.log("BROKEN: gauge level is", gauge.className);
    process.exit(1);
  }
  // The memory modal: empty, populated, and its count badge.
  api.renderMemory();
  api.state.memory = { path: "/p/.trance/memory.md", prompt_view: "- **backend**: port 3100",
                       raw: "- **backend**: port 3100",
                       notes: ["- **backend**: port 3100", "- a note with no author"] };
  api.renderMemory();
  api.paintMemoryCount();
  // Editing must hide the cards, and leaving it must bring them back.
  document.getElementById("memory-edit").onclick();
  if (!document.getElementById("memory-list").hidden
      || document.getElementById("memory-editor").hidden) {
    console.log("BROKEN: the memory editor does not replace the cards");
    process.exit(1);
  }
  document.getElementById("memory-cancel").onclick();
  if (document.getElementById("memory-list").hidden) {
    console.log("BROKEN: the cards did not come back after cancelling");
    process.exit(1);
  }
  api.renderStepSize();
  // The loops editor: an existing loop, and an empty one being created.
  // A replayed event must not touch the flow the snapshot just established.
  api.state.session.flow = { steps: [{ id: "keep", role: "backend", task: "t",
                                       status: "done", check: null, on_fail: null,
                                       max_loops: 2, attempts: [] }] };
  api.applyRefinedFlow({ flow: { steps: [{ id: "stale", role: "backend", task: "old",
                                           status: "pending", check: null, on_fail: null,
                                           max_loops: 2, attempts: [] }] } });
  if (api.state.session.flow.steps[0].id !== "stale") {
    console.log("BROKEN: a live flow_updated was ignored");
    process.exit(1);
  }

  api.state.loops = RESPONSES["/api/loops"];
  // The modal shows exactly one loop, chosen by name.
  await api.renderLoops("test-and-fix");
  {
    const shown = flat(document.getElementById("loop-list")).replace(/\s+/g, " ");
    if (shown.includes("reviewer")) {
      console.log("BROKEN: the loops modal rendered more than the selected loop");
      process.exit(1);
    }
    if (!shown.includes("on SUCCESS")) {
      console.log("BROKEN: the selected loop did not render");
      process.exit(1);
    }
  }
  api.state.commands = RESPONSES["/api/commands"];
  api.loopCard(JSON.parse(JSON.stringify(RESPONSES["/api/loops"].loops[0])), false);
  api.loopCard({ name: "", description: "", prompt: "", nodes: [], start: "",
                 max_steps: 12 }, true);
  // A loop step in the flow editor and in the run view.
  api.state.draftSteps = [{ id: "L1", role: "", loop: "test-and-fix", task: "t",
                            status: "pending", check: null, on_fail: null, max_loops: 2,
                            attempts: [], runs_a_loop: true }];
  api.redrawEditor ? api.redrawEditor() : api.renderFlowEditor();

  const withCheck = RESPONSES["/api/loops"].loops.find((l) => l.name === "test-and-fix");
  const card = api.loopCard(JSON.parse(JSON.stringify(withCheck)), false);
  const loopText = flat(card).replace(/\s+/g, " ");
  for (const want of ["on SUCCESS", "on FAILED", "on CHECK FAILED", "leave the loop"]) {
    if (!loopText.includes(want)) {
      console.log("BROKEN: the loop editor is missing", JSON.stringify(want));
      process.exit(1);
    }
  }
  // The plan is shown before splitting finishes, so both states must render.
  document.getElementById("screen-plan").classList.add("active");
  api.state.splitting = { count: 2, threshold: 5 };
  api.renderFlowEditor();
  api.state.draftSteps = [];
  api.renderFlowEditor();                    // the empty + splitting message
  api.state.splitting = null;

  // A refined flow must replace an untouched draft and spare an edited one.
  api.state.session.flow = { steps: [{ id: "a", role: "backend", task: "one", status: "pending",
                                       check: null, on_fail: null, max_loops: 2, attempts: [] }] };
  api.renderFlowEditor();
  const untouched = api.draftFingerprint();
  api.applyRefinedFlow({ flow: { steps: [
    { id: "b", role: "backend", task: "half one", status: "pending", check: null,
      on_fail: null, max_loops: 2, attempts: [] },
    { id: "c", role: "backend", task: "half two", status: "pending", check: null,
      on_fail: null, max_loops: 2, attempts: [] }] }, message: "Split into 2 steps." });
  if (api.state.draftSteps.length !== 2) {
    console.log("BROKEN: an untouched draft was not replaced by the split");
    process.exit(1);
  }
  api.state.draftSteps[0].task = "edited by hand";
  api.applyRefinedFlow({ flow: { steps: [{ id: "d", role: "backend", task: "other",
                                           status: "pending", check: null, on_fail: null,
                                           max_loops: 2, attempts: [] }] } });
  if (api.state.draftSteps[0].task !== "edited by hand") {
    console.log("BROKEN: a hand-edited draft was clobbered by a background split");
    process.exit(1);
  }
  // The race the user hit: a split lands while the chat response is still in
  // flight, and the older response must not put the un-split plan back.
  const versionBefore = api.state.flowVersion || 0;
  api.applyRefinedFlow({ flow: { steps: [{ id: "z", role: "backend", task: "split part",
                                           status: "pending", check: null, on_fail: null,
                                           max_loops: 2, attempts: [] }] } });
  if ((api.state.flowVersion || 0) === versionBefore) {
    console.log("BROKEN: a pushed flow did not bump the version a stale response checks");
    process.exit(1);
  }
  void untouched;
  // The badge must call out a step that is over the limit — that is its job.
  if (!api.pointsBadge({ points: 8 }).className.includes("over")) {
    console.log("BROKEN: an 8-point step is not flagged over a limit of 5");
    process.exit(1);
  }
  if (api.pointsBadge({ points: 3 }).className.includes("over")) {
    console.log("BROKEN: a 3-point step is flagged over a limit of 5");
    process.exit(1);
  }
  api.state.memory.oversized = true;      // the "over budget" branch
  api.renderMemory();
  console.log("context gauge:", text, "·", gauge.className);
  // Esc must close the topmost modal, and must not close one over a textarea.
  const modal = makeEl("div");
  modal.classList.add("open");
  openModals.push(modal);
  listeners.keydown({ key: "Escape", preventDefault() {} });
  if (modal.classList.contains("open")) {
    console.log("BROKEN: Escape did not close the open modal");
    process.exit(1);
  }
  modal.classList.add("open");
  const area = makeEl("textarea");
  modal.contains = () => true;
  document.activeElement = area;
  listeners.keydown({ key: "Escape", preventDefault() {} });
  if (!modal.classList.contains("open")) {
    console.log("BROKEN: Escape closed a modal while typing in a textarea");
    process.exit(1);
  }
  // The step detail: one folded section per block that ran.
  const loopStep = {
    id: "st1", role: "tester", loop: "test-and-fix", task: "make it pass",
    status: "done", check: null, on_fail: null, max_loops: 2, checker: null,
    attempts: [
      { n: 1, outcome: "FAILED", outcome_reason: "the ball passes through",
        files_written: [], gate_results: [],
        context: { tokens: 20000, window: 64000, budget: 55000, percent: 31 } },
      { n: 2, outcome: "SUCCESS", outcome_reason: "", files_written: ["game.js"],
        gate_results: [{ gate: "factchecker", verdict: "PASS" }], context: {} },
    ],
  };
  api.state.events = [
    { id: "e1", type: "loop_node", agent: "tester", step_id: "st1",
      ts: new Date().toISOString(),
      payload: { message: "test-and-fix: tester", visit: 1, role: "tester" } },
    { id: "e2", type: "model_call", agent: "tester", step_id: "st1",
      ts: new Date().toISOString(),
      payload: { model: "m", tool_calls: [], messages: [], summary: {} } },
    { id: "e3", type: "loop_node", agent: "backend", step_id: "st1",
      ts: new Date().toISOString(),
      payload: { message: "test-and-fix: backend", visit: 2, role: "backend" } },
    { id: "e4", type: "tool_call", agent: "backend", step_id: "st1",
      ts: new Date().toISOString(),
      payload: { name: "write_file", ok: true, arguments: {}, result: "",
                 detail: { kind: "write", path: "game.js", added: 2, removed: 0 } } },
  ];
  const grouped = api.groupStepEvents(api.state.events, loopStep);
  if (grouped.length !== 2 || grouped[0].agent !== "tester"
      || grouped[1].agent !== "backend") {
    console.log("BROKEN: step events were not grouped by block", grouped.length);
    process.exit(1);
  }
  if (grouped[0].events.length !== 2 || grouped[1].events.length !== 2) {
    console.log("BROKEN: events landed in the wrong block");
    process.exit(1);
  }
  api.openStep(loopStep, 0);
  const detail = flat(document.getElementById("step-body")).replace(/\s+/g, " ");
  for (const want of ["FAILED", "SUCCESS", "the ball passes through"]) {
    if (!detail.includes(want)) {
      console.log("BROKEN: the folded sections do not show", JSON.stringify(want));
      process.exit(1);
    }
  }
  // A step that ran before the page opened still shows its attempts.
  const noEvents = api.groupStepEvents([], loopStep);
  if (noEvents.length !== 2) {
    console.log("BROKEN: attempts without events produced no sections");
    process.exit(1);
  }
  console.log("all render paths ran without a ReferenceError");
  process.exit(0);
} catch (e) {
  console.log("BROKEN:", e.constructor.name + ":", e.message);
  process.exit(1);
}
})();
