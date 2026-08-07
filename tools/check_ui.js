// Loads app.js in a DOM-less harness and calls the entry points, so a call to
// a function that no longer exists fails here instead of in the browser.
const fs = require("fs");
const src = fs.readFileSync("src/trance/server/static/app.js", "utf8");

const nodes = new Map();
/* Appending is where a real DOM does two things a stub forgets: a selector can
   then find the child, and a <select> takes its value from its options. Both
   have hidden a bug here before. */
function adopt(parent, children) {
  for (const child of children) {
    if (child === undefined || child === null) continue;
    parent.children.push(child);
    for (const cls of String((child && child.className) || "").split(" ")) {
      if (cls) parent._found[`.${cls}`] = parent._found[`.${cls}`] || child;
    }
    if (parent.tagName === "SELECT" && child.tagName === "OPTION") {
      if (child.selected || parent.value === "") parent.value = child.value;
    }
  }
}

const makeEl = (tag = "div") => {
  let html = "";
  const el = {
    tagName: tag.toUpperCase(), className: "", style: {}, dataset: {}, children: [],
    textContent: "", value: "", checked: false, disabled: false,
    title: "", placeholder: "", type: "",
    append: (...c) => adopt(el, c), prepend: (...c) => adopt(el, c),
    remove: () => {},
    // Returns the child, as a real one does.
    appendChild: (c) => { adopt(el, [c]); return c; },
    addEventListener: () => {}, removeEventListener: () => {},
    // Null, like a real one that finds nothing. Returning an element made every
    // "is this here?" check pass, which is how a missing .c-head went unseen.
    querySelector: (sel) => el._found[sel] || null,
    querySelectorAll: () => [],
    get innerHTML() { return html; },
    // `box.innerHTML = ""` is how every panel here is emptied. A stub that kept
    // its children answered "is the old project still on screen?" with yes.
    set innerHTML(value) {
      html = value;
      if (!value) { el.children.length = 0; el._found = {}; el.textContent = ""; }
    },
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
  // A bare text node is a child like any other: `label.append(cb, text)` is how
  // every checkbox in this UI is labelled, so a stub that drops the text cannot
  // see whether any of them say anything.
  createElement: makeEl, createTextNode: (t) => ({ textContent: t, children: [] }),
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
  "/api/models/discover": { models: ["claude-opus-5", "claude-sonnet-5"], listed: true,
                            note: "2 from https://y/models", endpoint: "https://y" },
  "/api/presets": { presets: [
    { name: "Qwen3.6-llama.cpp", kind: "llamacpp", model: "qwen", base_url: "http://x/v1",
      context_window: 64000, max_tokens: 0, has_key: false, self_contained: true },
    { name: "claude", kind: "anthropic", model: "claude-opus-5", base_url: "https://y",
      context_window: 1000000, max_tokens: 0, has_key: true, self_contained: true }] },
  "/api/sessions/s1/files": {
    root: "/p",
    files: [{ path: "server/app.js", bytes: 2048, lines: 80 },
            { path: "server/public/index.html", bytes: 900, lines: 30 },
            { path: "README.md", bytes: 300, lines: 12 }],
    totals: [{ ext: "js", files: 1, lines: 80, bytes: 2048 },
             { ext: "html", files: 1, lines: 30, bytes: 900 },
             { ext: "md", files: 1, lines: 12, bytes: 300 }] },
  "/api/sessions/s1/preview": { root: "/p/server/public", port: 44817,
                                url: "http://192.168.10.59:44817/", needs_build: false,
                                local: "http://localhost:44817/", build_command: "",
                                public: "https://sterilize-unscathed.ngrok-free.dev/",
                                open: "http://localhost:44817/index.html" },
  "/api/sessions/s1/file?path=server%2Fapp.js": {
    path: "server/app.js", content: "const PORT = 3000;\napp.listen(PORT);\n",
    bytes: 40, lines: 2 },
  "/api/sessions/s1/review/changes": {
    review: "rev_1", status: "done", before: "aaa", after: "bbb",
    files: ["server/app.js"], notes: [{ path: "server/app.js", line: 1, note: "use env" }],
    diff: "--- a/server/app.js\n+++ b/server/app.js\n-const PORT = 3000;\n+const PORT = process.env.PORT;" },
  "/api/loops": { loops: [{ name: "another-loop", description: "d2", prompt: "",
                            start: "n_a", max_steps: 6, roles: ["reviewer"],
                            nodes: [{ id: "n_a", role: "reviewer", focus: "",
                                      check: null, revert_on_fail: false,
                                      on: { SUCCESS: { target: "exit", max_visits: 1 } } }] },
                          { name: "test-and-fix", description: "d", prompt: "p",
                            start: "n_test", max_steps: 10, roles: ["tester", "backend"],
                            nodes: [{ id: "n_test", role: "tester", focus: "run tests",
                                      check: null,
                                      on: { SUCCESS: [{ target: "exit", max_visits: 3 }],
                                            FAILED: [{ target: "n_fix", max_visits: 2 },
                                                     { target: "n_fix", max_visits: 2,
                                                       backup: true }] } },
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
// Every request the UI makes, so a check can ask "did that reach the server?"
// — the only way to see a save that happens without a button being pressed.
const REQUESTS = [];
global.fetch = async (path, init = {}) => {
  REQUESTS.push({
    url: path, method: (init.method || "GET").toUpperCase(),
    body: init.body ? JSON.parse(init.body) : null,
  });
  return { ok: true, status: 200, json: async () => RESPONSES[path] ?? {} };
};

const module_ = { exports: {} };
new Function("module", "exports", src + "\n;module.exports={state,openSession,renderRun,renderFlowView,renderChat,renderFlowEditor,stepCard,consoleAppend,trackActivity,consoleReset,paintPaused,renderSessionBar,trackClock,renderFlowEditor,redrawEditor,openStep,groupStepEvents,agentCard,contextGauge,renderMemory,openMemory,loadMemory,paintMemoryCount,renderStepSize,openSettings,renderPresets,cloneLoop,pointsBadge,applyRefinedFlow,refreshPlan,saveFlowNow,queueFlowSave,draftFingerprint,loopCard,renderLoops,openFiles,openFile,renderFileTree,renderFileView,commentOn,renderReviewStatus,renderPreviewStatus,warnAboutBuild,startShare,resetFiles,closeFile,renderGeneralComment,filePath:()=>fileState.path};")(module_, module_.exports);
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
// Values count as visible text: a model name lives in an input, not a label.
const flat = (n) => !n ? ""
  : [n.textContent || "", n.value || "", n.placeholder || ""].join(" ")
    + (n.children || []).map(flat).join(" ");
(async () => {
try {
  // state lives inside the module scope; exercise the renderers that read it
  // Render a real session with steps in every status — an empty flow used to
  // skip stepCard entirely, which is how a ReferenceError in it went unnoticed.
  api.state.session = session;
  api.state.roles = { backend: { name: "backend", color: "#7aa2f7", verifier: false },
                      tester: { name: "tester", color: "#f7768e", verifier: true } };
  // An agent card must offer the defined models and nothing else.
  api.state.presets = RESPONSES["/api/presets"].presets;
  {
    const card = api.agentCard({ name: "backend", title: "Backend", description: "d",
                                 system_prompt: "p", paths: [], toolsets: ["files"],
                                 preset: null, color: "#7aa2f7", commands: [],
                                 command_list: "", verifier: false }, false);
    const text = flat(card).replace(/\s+/g, " ");
    if (text.includes("default model")) {
      console.log("BROKEN: the agent model picker still offers a phantom default");
      process.exit(1);
    }
    if (!text.includes("Qwen3.6-llama.cpp") || !text.includes("claude")) {
      console.log("BROKEN: the agent model picker does not list the defined models");
      process.exit(1);
    }
    if (!text.includes("Backup model") || !text.includes("keep trying the same model")) {
      console.log("BROKEN: the agent card has no backup model picker");
      process.exit(1);
    }
    if (text.includes("Shown to you, and to the orchestrator")) {
      console.log("BROKEN: the description hint is back");
      process.exit(1);
    }
  }
  {
    // An agent with a backup says so on its header, folded or not.
    const card = api.agentCard({ name: "backend", title: "Backend", description: "d",
                                 system_prompt: "p", paths: [], toolsets: ["files"],
                                 preset: "Qwen3.6-llama.cpp", backup_preset: "claude",
                                 tries: 2, backup_tries: 2, color: "#7aa2f7",
                                 commands: [], command_list: "", verifier: false }, false);
    const text = flat(card).replace(/\s+/g, " ");
    if (!text.includes("2 tries, then 2 on claude")) {
      console.log("BROKEN: an agent's backup is not shown on its card");
      process.exit(1);
    }
    if (!text.includes("4") || !text.includes("tries in all")
        || !text.includes("2 on the model, then 2 on the backup")) {
      console.log("BROKEN: the card does not spell out where the tries go");
      process.exit(1);
    }
  }
  api.state.planning = { max_step_points: 5, scale: [1, 2, 3, 5, 8, 13] };
  // One step mid-split: the marker belongs on that card, not above the plan.
  api.state.splitting = { count: 1, threshold: 5, step_ids: ["s4"] };
  api.state.roles.backend.tries = 2;
  api.state.roles.backend.backup_preset = "claude";
  api.state.roles.backend.backup_tries = 2;
  api.state.draftSteps = ["pending", "running", "done", "failed", "skipped", "blocked"]
    .map((status, i) => ({ id: `s${i}`, role: "backend", task: `task ${i}`, status,
                           check: "tester", on_fail: null, max_loops: 0, checker: "tester",
                           fixer: "backend", loop_limit: 4, attempts: [],
                           points: [0, 1, 3, 5, 8, 13][i] }));
  api.state.draftSteps.forEach((step, i) => api.stepCard(step, i));   // every status
  {
    // A step with no override shows the agent's own count (2 + 2 on its backup).
    const card = api.stepCard(api.state.draftSteps[0], 0);
    const text = flat(card).replace(/\s+/g, " ");
    if (!text.includes("4 tries in all") || !text.includes("backend's own")) {
      console.log("BROKEN: a step does not fall back to its agent's try count");
      process.exit(1);
    }
    const fixed = { ...api.state.draftSteps[0], max_loops: 6 };
    if (!flat(api.stepCard(fixed, 0)).replace(/\s+/g, " ").includes("6 tries in all")) {
      console.log("BROKEN: a step's own try count is ignored");
      process.exit(1);
    }
  }
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

  // The run clock: a total that ticks while working and freezes when not.
  api.trackClock({ run_seconds: 125, working: true });
  api.renderRun();
  {
    const shown = flat(document.getElementById("run-status")).replace(/\s+/g, " ");
    if (!shown.includes("worked") || !shown.includes("2m 05s")) {
      console.log("BROKEN: the run clock is missing or wrong:", shown.slice(0, 120));
      process.exit(1);
    }
  }
  api.trackClock({ run_seconds: 3725, working: false });
  api.renderRun();
  if (!flat(document.getElementById("run-status")).includes("1h 02m")) {
    console.log("BROKEN: the run clock does not carry hours");
    process.exit(1);
  }
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
                        { kind: "edit_miss", path: "server/app.js" },
                        { kind: "edit_ambiguous", path: "server/app.js", count: 3 },
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
  // Opening settings must actually list the models and the orchestrator.
  await api.openSettings();
  {
    const shown = flat(document.getElementById("preset-list")).replace(/\s+/g, " ");
    if (!shown.includes("Qwen3.6-llama.cpp") || !shown.includes("claude")) {
      console.log("BROKEN: the models list is empty when settings opens");
      process.exit(1);
    }
    // Discovery runs on render; the suggestions land under the model field.
    await new Promise((r) => setTimeout(r, 0));
    if (!shown.includes("model id")) {
      console.log("BROKEN: the model field lost its placeholder");
      process.exit(1);
    }
    const orch = flat(document.getElementById("orchestrator-settings")).replace(/\s+/g, " ");
    if (!orch.includes("claude")) {
      console.log("BROKEN: the orchestrator picker is empty when settings opens");
      process.exit(1);
    }
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

  // Editing saves itself: there is no Save button to press, so an edit that
  // does not reach the server is silently lost.
  {
    api.state.session.status = "planning";
    api.state.session.flow = { steps: [{ id: "one", role: "backend", task: "t",
                                         status: "pending", check: null, on_fail: null,
                                         max_loops: 2, attempts: [] }] };
    api.renderFlowEditor();
    const before = REQUESTS.filter((r) => r.method === "PUT").length;
    api.state.draftSteps.push({ role: "backend", loop: "", task: "added",
                                check: null, on_fail: null, max_loops: 2,
                                status: "pending" });
    await api.saveFlowNow();
    const puts = REQUESTS.filter((r) => r.method === "PUT" && /\/flow$/.test(r.url));
    if (puts.length <= before) {
      console.log("BROKEN: an edit never reached the server");
      process.exit(1);
    }
    if ((puts[puts.length - 1].body.steps || []).length !== 2) {
      console.log("BROKEN: the saved flow is not what was edited");
      process.exit(1);
    }
    const mark = flat(document.getElementById("flow-saved"));
    if (!mark.includes("saved")) {
      console.log("BROKEN: nothing says the flow was saved:", mark);
      process.exit(1);
    }
  }

  // A step added elsewhere — a review sent, the orchestrator finishing — has to
  // reach the plan screen, which edits a copy of the flow rather than the flow.
  {
    api.state.session.status = "planning";
    api.state.session.flow = { steps: [{ id: "one", role: "backend", task: "t",
                                         status: "pending", check: null, on_fail: null,
                                         max_loops: 2, attempts: [] }] };
    api.renderFlowEditor();
    api.state.session.flow.steps.push({ id: "two", role: "", loop: "review-front-end",
                                        task: "address the review", status: "pending",
                                        check: null, on_fail: null, max_loops: 2,
                                        attempts: [], runs_a_loop: true });
    api.refreshPlan();
    if ((api.state.draftSteps || []).length !== 2) {
      console.log("BROKEN: a step added on the server never reached the plan screen");
      process.exit(1);
    }
    // ...but not over the top of something you were in the middle of writing.
    api.state.draftSteps[0].task = "half-typed edit";
    api.state.session.flow.steps.push({ id: "three", role: "backend", task: "later",
                                        status: "pending", check: null, on_fail: null,
                                        max_loops: 2, attempts: [] });
    api.refreshPlan();
    if (api.state.draftSteps.length !== 2 ||
        api.state.draftSteps[0].task !== "half-typed edit") {
      console.log("BROKEN: an unsaved edit was overwritten by a server update");
      process.exit(1);
    }
  }

  // The activity line names the step from the flow, so it cannot contradict
  // the marker on the card — three steps running the same loop look alike.
  {
    api.state.session.flow = { steps: [
      { id: "a", role: "backend", task: "one", status: "done", attempts: [] },
      { id: "b", role: "", loop: "review", task: "two", status: "running", attempts: [] },
      { id: "c", role: "", loop: "review", task: "three", status: "pending", attempts: [] },
    ] };
    api.trackActivity({ type: "model_call", agent: "reviewer",
                        payload: { model: "m", tool_calls: [] } });
    const line = flat(document.getElementById("now-working")).replace(/\s+/g, " ");
    if (!line.includes("step 2/3")) {
      console.log("BROKEN: the activity line does not say which step:", line.slice(0, 120));
      process.exit(1);
    }
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
    // A tiered exit has to say which turns each arrow covers, and where it ends.
    for (const want of ["1st–2nd", "3rd–4th", "backup model", "after 4, the loop halts"]) {
      if (!shown.includes(want)) {
        console.log(`BROKEN: a tiered exit does not show ${want}:`, shown.slice(-400));
        process.exit(1);
      }
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
  // Cloning: a new name, new block ids, and arrows that stay inside the copy.
  {
    const original = RESPONSES["/api/loops"].loops.find((l) => l.name === "test-and-fix");
    const copy = api.cloneLoop(original);
    if (copy.name === original.name) {
      console.log("BROKEN: a cloned loop kept the original's name");
      process.exit(1);
    }
    const ids = new Set(copy.nodes.map((n) => n.id));
    if (copy.nodes.some((n) => original.nodes.some((o) => o.id === n.id))) {
      console.log("BROKEN: a cloned loop reused the original's block ids");
      process.exit(1);
    }
    for (const node of copy.nodes) {
      for (const routes of Object.values(node.on || {})) {
        for (const edge of (Array.isArray(routes) ? routes : [routes])) {
          if (!["exit", "fail"].includes(edge.target) && !ids.has(edge.target)) {
            console.log("BROKEN: a cloned loop's arrow points outside the copy");
            process.exit(1);
          }
        }
      }
    }
    if (!ids.has(copy.start)) {
      console.log("BROKEN: a cloned loop starts nowhere");
      process.exit(1);
    }
    if (original.nodes[0].id.startsWith("n_") === false) {
      console.log("BROKEN: the fixture changed shape");
      process.exit(1);
    }
  }

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
  // A step whose agent has a backup offers a rerun straight onto it. (Roles are
  // re-set here because opening settings reloads them from /api/config.)
  api.state.roles = {
    backend: { name: "backend", color: "#7aa2f7", verifier: false,
               tries: 2, backup_preset: "claude", backup_tries: 2 },
    tester: { name: "tester", color: "#f7768e", verifier: true },
  };
  api.openStep({ ...loopStep, loop: "", role: "backend" }, 0);
  {
    const shown = flat(document.getElementById("step-body")).replace(/\s+/g, " ");
    if (!shown.includes("rerun on claude")) {
      console.log("BROKEN: no way to rerun a step on its backup model");
      process.exit(1);
    }
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
  // The files screen: tree, a file open, a comment on a line.
  api.state.session.review = [];
  await api.openFiles();
  {
    // Nothing selected: the pane is the comment box straight away, not after a
    // file has been opened and closed to force it.
    const pane = flat(document.getElementById("file-view")).replace(/\s+/g, " ");
    if (!pane.includes("comment about the whole thing")) {
      console.log("BROKEN: the files screen opened without the comment box:",
                  pane.slice(0, 150));
      process.exit(1);
    }
  }
  await api.openFile("server/app.js");
  {
    const tree = flat(document.getElementById("file-tree")).replace(/\s+/g, " ");
    if (!tree.includes("app.js")) {
      console.log("BROKEN: the file tree did not render");
      process.exit(1);
    }
    // The harness has no real tree, so each id is its own node: read the body.
    const stats = flat(document.getElementById("file-stats-body")).replace(/\s+/g, " ");
    for (const want of [".js", "80", ".html", "30", ".md", "12"]) {
      if (!stats.includes(want)) {
        console.log("BROKEN: file statistics missing", want, "in:", stats.slice(0, 120));
        process.exit(1);
      }
    }
    if (!tree.includes("▷")) {
      console.log("BROKEN: no way to open a page from the tree");
      process.exit(1);
    }
    if (/\d+L/.test(tree)) {
      console.log("BROKEN: per-file line counts are back in the tree");
      process.exit(1);
    }
    // A page that cannot run as files explains itself before opening.
    api.warnAboutBuild({ root: "/p", build_command: "npm run dev", needs_build: true,
                         url: "http://192.168.10.59:44817/",
                         blocked_by: [{ file: "src/main.js", specifier: "three", line: 1 }] });
    const warning = flat(document.getElementById("preview-warning-body"))
                      .replace(/\s+/g, " ");
    for (const want of ["src/main.js:1", "'three'", "npm run dev"]) {
      if (!warning.includes(want)) {
        console.log(`BROKEN: the build warning does not say ${want}:`, warning.slice(0, 200));
        process.exit(1);
      }
    }

    // A dev server reports its own URL, not a port trance picked.
    api.renderPreviewStatus(RESPONSES["/api/sessions/s1/preview"]);
    // Writing a review comment re-renders the review status. That must not take
    // the share link off screen — they used to share one container.
    api.renderReviewStatus();
    const status = flat(document.getElementById("preview-status")).replace(/\s+/g, " ");
    // With a tunnel up: the link, and a way to close it. Without one: an
    // offer to start one, never a public URL you did not ask for.
    api.state.shared = { running: true, url: "https://sterilize-unscathed.ngrok-free.dev/" };
    api.renderPreviewStatus();
    const shared = flat(document.getElementById("preview-status")).replace(/\s+/g, " ");
    if (!shared.includes("share") || !shared.includes("stop sharing")) {
      console.log("BROKEN: no way to stop sharing:", shared.slice(0, 140));
      process.exit(1);
    }
    api.state.shared = null;
    api.renderPreviewStatus({ ...RESPONSES["/api/sessions/s1/preview"], public: "" });
    const unshared = flat(document.getElementById("preview-status")).replace(/\s+/g, " ");
    if (!unshared.includes("share…") || unshared.includes("ngrok-free")) {
      console.log("BROKEN: sharing is not offered, or leaked a URL:",
                  unshared.slice(0, 140));
      process.exit(1);
    }
    api.renderPreviewStatus(RESPONSES["/api/sessions/s1/preview"]);

    // The address shown is the one you can type into a phone.
    if (!status.includes("serving") || !status.includes("192.168.10.59:44817")) {
      console.log("BROKEN: the running preview is not shown:", status.slice(0, 100));
      process.exit(1);
    }
    // With a tunnel up, the link worth sending someone is on screen.
    if (!status.includes("share")) {
      console.log("BROKEN: the public link is not offered:", status.slice(0, 120));
      process.exit(1);
    }
    const view = flat(document.getElementById("file-view")).replace(/\s+/g, " ");
    if (!view.includes("const PORT = 3000;") || !view.includes("1")) {
      console.log("BROKEN: the file view did not render its lines");
      process.exit(1);
    }
  }
  api.state.session.review = [{ id: "rv_1", path: "server/app.js", line: 1,
                               note: "read the port from the environment" }];
  api.renderFileView();
  api.renderReviewStatus();
  {
    const view = flat(document.getElementById("file-view")).replace(/\s+/g, " ");
    if (!view.includes("read the port from the environment")) {
      console.log("BROKEN: a review comment is not shown against its line");
      process.exit(1);
    }
  }
  api.commentOn(2, "app.listen(PORT);");

  // Clicking the open file again closes it, and the pane it leaves behind is
  // where a comment about the whole thing is written.
  api.state.session.review = [
    { id: "rv_1", path: "server/app.js", line: 1, note: "read the port from the environment" },
    { id: "rv_2", path: "", line: 0, note: "the controls are unusable on a phone" },
  ];
  await api.openFile("server/app.js");        // the same path: closes it
  if (api.state.filePath && api.state.filePath()) {
    console.log("BROKEN: clicking the open file again did not close it");
    process.exit(1);
  }
  {
    const pane = flat(document.getElementById("file-view")).replace(/\s+/g, " ");
    for (const want of ["comment about the whole thing", "Add comment",
                        "server/app.js:1", "overall", "unusable on a phone"]) {
      if (!pane.includes(want)) {
        console.log("BROKEN: the general comment pane is missing", want, ":",
                    pane.slice(0, 200));
        process.exit(1);
      }
    }
  }

  // Switching to another project must not leave the last one's numbers up:
  // "24 files, 6,426 lines" from the project you just left reads as an answer
  // about the project you are now looking at.
  // openSession() replaces state.session before it resets the files pane, so a
  // switch means both: a different session, and nothing of the old one left.
  api.state.session = { ...api.state.session, id: "s2", review: [] };
  api.resetFiles();
  {
    const stats = flat(document.getElementById("file-stats-body")).replace(/\s+/g, " ");
    if (/\d/.test(stats)) {
      console.log("BROKEN: file statistics survived a session change:", stats.slice(0, 120));
      process.exit(1);
    }
    if (flat(document.getElementById("file-tree")).trim()) {
      console.log("BROKEN: the file tree survived a session change");
      process.exit(1);
    }
    // The pane comes back as the comment box — but with none of the previous
    // project's file in it, and none of its comments.
    const pane = flat(document.getElementById("file-view")).replace(/\s+/g, " ");
    // "unusable on a phone" is also the textarea's placeholder, so the marker
    // for "a comment came with us" is the list heading, not the text.
    if (pane.includes("const PORT = 3000;") || pane.includes("Waiting to be sent")) {
      console.log("BROKEN: the open file survived a session change:", pane.slice(0, 150));
      process.exit(1);
    }
  }
  // With no CodeMirror (this harness, or a browser that failed to load it) the
  // plain numbered view still shows the file and takes comments.
  if (typeof CodeMirror !== "undefined") {
    console.log("BROKEN: the harness unexpectedly has CodeMirror");
    process.exit(1);
  }
  console.log("all render paths ran without a ReferenceError");
  process.exit(0);
} catch (e) {
  console.log("BROKEN:", e.constructor.name + ":", e.message);
  process.exit(1);
}
})();
