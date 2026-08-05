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
new Function("module", "exports", src + "\n;module.exports={openSession,renderRun,renderFlowView,renderChat,renderFlowEditor,consoleAppend,trackActivity,consoleReset,paintPaused,renderSessionBar};")(module_, module_.exports);
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
  api.trackActivity({ type: "model_call", agent: "backend", ts: new Date().toISOString(),
                      payload: { model: "m", tool_calls: [], messages: [], summary: {} } });
  console.log("all render paths ran without a ReferenceError");
  process.exit(0);
} catch (e) {
  console.log("BROKEN:", e.constructor.name + ":", e.message);
  process.exit(1);
}
