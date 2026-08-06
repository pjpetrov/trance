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
    querySelector: () => makeEl(), querySelectorAll: () => [],
    classList: { add: () => {}, remove: () => {}, toggle: () => {}, contains: () => false },
    focus: () => {}, select: () => {}, setAttribute: () => {},
    scrollTop: 0, scrollHeight: 0, clientHeight: 0,
    get firstChild() { return el.children[0] || makeEl(); },
  };
  return el;
};
global.document = {
  getElementById: (id) => { if (!nodes.has(id)) nodes.set(id, makeEl()); return nodes.get(id); },
  createElement: makeEl, createTextNode: (t) => ({ text: t }),
  createElementNS: (_ns, tag) => makeEl(tag),
  querySelectorAll: () => [], body: makeEl(),
};
global.window = { isSecureContext: false };
global.navigator = {};
global.location = { protocol: "http:", host: "x" };
global.WebSocket = function () { return { onmessage: null, onclose: null, close() {} }; };
const RESPONSES = {
  "/api/config": { roles: {}, providers: [], presets: [], kinds: {},
                   orchestrator: { provider: "p", model: "m", base_url: "u" } },
  "/api/workspace": { workspace: "/w", writable: true, suggested_name: "project",
                      suggested_dir: "/w/project", state_dir: "/s" },
  "/api/sessions": [],
};
global.fetch = async (path) => ({
  ok: true, status: 200,
  json: async () => RESPONSES[path] ?? {},
});

const module_ = { exports: {} };
new Function("module", "exports", src + "\n;module.exports={state,openSession,renderRun,renderFlowView,renderChat,renderFlowEditor,stepCard,consoleAppend,trackActivity,consoleReset,paintPaused,renderSessionBar,contextGauge};")(module_, module_.exports);
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
try {
  // state lives inside the module scope; exercise the renderers that read it
  // Render a real session with steps in every status — an empty flow used to
  // skip stepCard entirely, which is how a ReferenceError in it went unnoticed.
  api.state.session = session;
  api.state.roles = { backend: { name: "backend", color: "#7aa2f7", verifier: false },
                      tester: { name: "tester", color: "#f7768e", verifier: true } };
  api.state.draftSteps = ["pending", "running", "done", "failed", "skipped", "blocked"]
    .map((status, i) => ({ id: `s${i}`, role: "backend", task: `task ${i}`, status,
                           check: "tester", on_fail: null, max_loops: 2, checker: "tester",
                           fixer: "backend", loop_limit: 2, attempts: [] }));
  api.state.draftSteps.forEach((step, i) => api.stepCard(step, i));   // every status
  api.renderSessionBar(); api.renderChat(); api.renderFlowEditor();
  api.renderFlowView(); api.renderRun(); api.paintPaused();
  api.consoleReset();
  api.consoleAppend({ type: "step_started", agent: "backend", step_id: "st1",
                      ts: new Date().toISOString(), payload: { task: "t", attempt: 1 } });
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
  const flat = (n) => (n.textContent || "") + (n.children || []).map(flat).join(" ");
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
  console.log("context gauge:", text, "·", gauge.className);
  console.log("all render paths ran without a ReferenceError");
  process.exit(0);
} catch (e) {
  console.log("BROKEN:", e.constructor.name + ":", e.message);
  process.exit(1);
}
