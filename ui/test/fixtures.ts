/** Sample payloads, shaped like the real ones.
 *
 * Copied from live responses rather than invented, because a fixture that has
 * drifted from the API is worse than none: it makes the tests pass on a shape
 * the server stopped sending. Every field here exists in a real response.
 */

import type {
  AgentRole, AppConfig, ModelPreset, Session, Step, ToolDetail, TranceEvent,
} from "@/api/types";

export function step(over: Partial<Step> = {}): Step {
  return {
    id: "st1", role: "frontend", task: "Build the maze renderer", loop: "",
    check: "", checks: [], checker: "", fixer: "", on_fail: null, verify_with: null,
    max_loops: 2, loop_limit: 0, max_attempts: 2, overrides_tries: false,
    start_on_backup: false, revert_on_fail: false, escalated: false, points: 3,
    gates: [], entry: "", status: "pending", attempts: [], runs: 0, steering: [],
    summary: "", runs_a_loop: false, ...over,
  };
}

export function session(over: Partial<Session> = {}): Session {
  return {
    id: "s1", name: "pacman", project_dir: "/w/pacman", status: "ready",
    goal: "A Pac-Man clone", paused: false, working: false, run_seconds: 0,
    created_at: "2026-08-09T10:00:00Z", error: null, chat: [], team: [], history: [],
    review: [], reviews: [],
    flow: { steps: [step()], cursor: 0 },
    progress: { total: 1, done: 0, failed: 0, running: 0 },
    resolved_models: {},
    ...over,
  };
}

export function role(over: Partial<AgentRole> = {}): AgentRole {
  return {
    name: "frontend", title: "Frontend engineer", description: "client-side code",
    system_prompt: "You are a frontend engineer.", paths: ["src/**"],
    toolsets: ["files", "graph"], commands: [], command_list: "", workdir: "",
    shell: null, verifier: false, preset: "Qwen", backup_preset: null, tries: 2,
    backup_tries: 2, tool_rounds: 24, color: "#9ece6a", ...over,
  };
}

export function preset(over: Partial<ModelPreset> = {}): ModelPreset {
  return {
    name: "Qwen", kind: "llamacpp", model: "qwen3.6", base_url: "http://x/v1",
    context_window: 64000, max_tokens: 0, has_key: false, self_contained: true, ...over,
  };
}

export function config(over: Partial<AppConfig> = {}): AppConfig {
  return {
    config: {},
    presets: [preset()],
    kinds: {},
    scale: [1, 2, 3, 5, 8, 13],
    visual: { browser: true },
    orchestrator: { preset: "Qwen", provider: "p", model: "qwen3.6" },
    stale: false,
    ...over,
  };
}

let sequence = 0;

export function event(detail: ToolDetail, over: Partial<TranceEvent> = {}): TranceEvent {
  sequence += 1;
  return {
    id: `ev${sequence}`, type: "tool_call", session_id: "s1",
    ts: "2026-08-09T10:00:00Z", agent: "visual-tester", step_id: "st1",
    payload: { name: "tool", ok: true, result: "the tool's own words", detail },
    ...over,
  };
}

/** The events endpoint answers two different shapes: `?tail=true` returns
 *  {events,total,shown} and `?step=…` returns a bare array. Conflating them in
 *  a fixture is how a test ends up asserting against a shape the UI never
 *  receives, so the split lives here rather than in each test. */
export function eventsRoute(tail: TranceEvent[], perStep: TranceEvent[] = []) {
  return ({ url }: { url: string }) =>
    (url.includes("step=") ? perStep : { events: tail, total: tail.length, shown: tail.length });
}

/** GET /api/sessions/{id}/settings — a project's own run settings. */
export function settings(over: Partial<Record<string, unknown>> = {}) {
  return {
    max_step_points: 5, escalation_preset: "", escalation_role: "",
    git_commits: true, git_auto_init: true,
    scale: [1, 2, 3, 5, 8, 13], migrated: false, ...over,
  };
}
