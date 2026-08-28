/** The shapes the FastAPI side actually returns.
 *
 * Taken from live responses rather than from reading the Python, because the
 * two have disagreed before — `resolved_models` is keyed by role name, `paths`
 * on a role is a glob list and not a path list, and an event's `detail` is a
 * discriminated union that the old UI decoded with a chain of `if`s and no way
 * to know when it had missed a case.
 *
 * Anything optional here is optional because the server genuinely omits it,
 * usually because the payload was slimmed for the history panel.
 */

export type SessionStatus =
  | "ready" | "running" | "paused" | "finished" | "error" | "halted";

export type StepStatus =
  // "verifying" and "blocked" come out of the engine like any other. Leaving
  // them off this list did not stop them arriving — it made them render as
  // the default, so work that succeeded but could not be verified looked
  // exactly like work that had never run.
  | "pending" | "running" | "verifying" | "done" | "blocked"
  | "failed" | "skipped" | "halted";

export type Outcome = "SUCCESS" | "FAILED" | "UNCLEAR" | "UNSTATED";
export type Verdict = "PASS" | "FAIL";

// ----------------------------------------------------------------- agents

export type Toolset = "files" | "graph" | "commands" | "inspect" | "browser";

export interface AgentRole {
  name: string;
  title: string;
  description: string;
  system_prompt: string;
  /** Globs this role may write to. Empty means read-only, which is a choice. */
  paths: string[];
  toolsets: Toolset[];
  commands: string[];
  command_list: string;
  workdir: string;
  shell: boolean | null;
  verifier: boolean;
  /** Verifiers that run after every step this agent does, on top of whatever
   *  the plan put on the step itself. */
  checks?: string[];
  preset: string | null;
  backup_preset: string | null;
  tries: number;
  backup_tries: number;
  tool_rounds: number;
  color: string;
  /** Off = kept but out of play: not plannable, its checks skipped, a step
   *  naming it fails saying so. */
  enabled?: boolean;
  protected?: boolean;
  /** Definition fields that differ from what a reset would restore — the
   *  Default copy in a session, shipped in the Default scope. */
  differs?: string[];
  resolved?: ResolvedModel;
}

export interface ResolvedModel {
  preset?: string;
  provider: string;
  model: string;
  base_url?: string;
  context_window?: number;
}

// ----------------------------------------------------------------- models

export interface Spend {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  total: number;
}

export interface ModelPreset {
  name: string;
  kind: string;
  model: string;
  base_url: string;
  api_key?: string | null;
  context_window: number;
  max_tokens: number;
  /** What cuts a reply that goes long: "time" (wall clock) or "size"
   *  (max output tokens only). Empty means time. */
  cap?: "" | "time" | "size";
  /** Wall-clock budget in seconds for one generation when cap is time.
   *  0 inherits the default (600). */
  timeout_s?: number;
  description?: string;
  has_key: boolean;
  self_contained: boolean;
  /** The fallback model for any agent whose own preset is missing. */
  default?: boolean;
  spend?: Spend | null;
}

export interface KindDefault {
  label: string;
  base_url: string;
  context_window: number;
  needs_key: boolean;
  models?: string[];
}

// ------------------------------------------------------------------ flow

export interface ContextUsage {
  tokens: number;
  window: number;
  budget: number;
  reserved: number;
  percent: number;
  estimated: boolean;
}

export interface GateResult {
  gate: string;
  verdict: Verdict | string;
  feedback: string;
  event_id: string | null;
}

export interface Attempt {
  n: number;
  worker_event_id: string | null;
  verifier_event_id: string | null;
  verdict: string;
  feedback: string;
  files_written: string[];
  gate_results: GateResult[];
  fix_event_id: string | null;
  fix_summary: string;
  refused_paths: string[];
  context?: ContextUsage;
  on_backup: boolean;
  checkpoint: string;
  commit: string;
  reverted: boolean;
  outcome: string;
  outcome_reason: string;
}

export interface Step {
  id: string;
  role: string;
  task: string;
  /** Set when the step runs a loop instead of a single agent. */
  loop: string;
  check: string;
  checks: string[];
  checker: string;
  fixer: string;
  on_fail: string | null;
  verify_with: string | null;
  max_loops: number;
  loop_limit: number;
  max_attempts: number;
  overrides_tries: boolean;
  start_on_backup: boolean;
  revert_on_fail: boolean;
  escalated: boolean;
  points: number;
  gates: string[];
  entry: string;
  status: StepStatus;
  /** Working time this step has cost, across attempts, fixes and checks. */
  seconds?: number;
  /** The inverse commit of the last user revert, when one is outstanding. */
  reverted_sha?: string;
  /** Screenshots attached to the request this step came from. */
  images?: string[];
  attempts: Attempt[];
  /** Executions, not attempts: one press of Start or Rerun, holding every
   *  retry and every loop block inside it. */
  runs: number;
  steering: string[];
  summary: string;
  runs_a_loop: boolean;
}

export interface Flow {
  steps: Step[];
  cursor: number;
}

export interface Progress {
  total: number;
  done: number;
  failed: number;
  running: number;
}

// --------------------------------------------------------------- session

export interface ChatMessage {
  /** Addressable, so a reply can be pointed at from elsewhere. */
  id: string;
  role: "user" | "orchestrator" | string;
  content: string;
  ts?: string;
  /** On an orchestrator reply that proposed work: where the code stood when it
   *  said so. Its presence is what makes "show me what came of this" offerable. */
  base?: string;
  /** The steps that reply added, by id. */
  steps?: string[];
  /** Screenshots attached to this message, as paths under .trance/shots. */
  images?: string[];
}

export interface ReviewComment {
  /** The server addresses a note by this, not by its position in the list —
   *  dropping one by index deletes nothing and answers 404. */
  id: string;
  path: string;
  /** 0 when the note is about the project rather than a line. */
  line: number;
  code: string;
  note: string;
}

export interface Session {
  id: string;
  name: string;
  project_dir: string;
  status: SessionStatus;
  goal: string;
  paused: boolean;
  working: boolean;
  run_seconds: number;
  /** Working time by agent name — who the run_seconds went to. */
  agent_seconds?: Record<string, number>;
  created_at: string;
  error: string | null;
  /** The per-run thinking switch: true = later calls go out without it. */
  thinking_disabled?: boolean;
  chat: ChatMessage[];
  team: AgentRole[];
  history: unknown[];
  review: ReviewComment[];
  reviews: unknown[];
  flow: Flow;
  progress: Progress;
  /** Keyed by role name. */
  resolved_models: Record<string, ResolvedModel>;
}

/** A question the run is blocked on: an agent wants to do something its
 *  permissions do not cover, and nothing happens until this is answered. */
export interface Approval {
  id: string;
  kind: string;
  agent: string;
  step_id: string;
  /** The command, or the path — what is actually being asked for. */
  subject: string;
  detail: { programs?: string[]; agent_has_own_list?: boolean; path?: string };
  decision: string;
  message?: string;
}

/** One iteration in the request history: the card the commits page draws
 *  collapsed. The expanded detail comes from MessageCommits. */
export interface RequestItem {
  reply_id: string;
  ts: string;
  request: string;
  base: string;
  after: string;
  commit_count: number;
  file_count: number;
  still_to_run: number;
  worked_seconds: number;
  /** Paths under .trance/shots — the user's attachments, then the run's. */
  shots: string[];
}

/** What one request turned into: the steps it added and the commits they made. */
export interface MessageCommits {
  message: { id: string; role: string; content: string; ts: string };
  base: string;
  after: string;
  steps: Step[];
  still_to_run: number;
  commits: { sha: string; short: string; subject: string; when: string; who: string }[];
  files: string[];
}

// ---------------------------------------------------------------- events

/** The structured half of a tool_call, which is what the console renders.
 *  A union rather than a bag, so a new kind is a compile error at every
 *  place that has to decide how to draw it. */
export type ToolDetail =
  | { kind: "write"; path: string; created?: boolean; appended?: boolean;
      added: number; removed: number; diff: string; truncated?: boolean }
  | { kind: "read"; path: string; bytes?: number; lines?: number;
      start_line?: number; last_line?: number; outline?: boolean;
      symbols?: number; deduped?: boolean }
  | { kind: "graph"; tool: string; hit: boolean; query: string; deduped?: boolean }
  | { kind: "command"; command: string; exit_code: number; output: string;
      seconds: number; timed_out?: boolean; cancelled?: boolean }
  | { kind: "background"; command: string; command_id: string }
  | { kind: "command_stopped"; command_id: string }
  | { kind: "memory"; note: string; stored: boolean }
  | { kind: "check"; files: FileStat[] }
  | { kind: "truncated"; limit: number }
  | { kind: "malformed"; raw: string }
  | { kind: "edit_miss"; path?: string; symbol?: string; count?: number }
  | { kind: "edit_ambiguous"; path?: string; symbol?: string; count?: number }
  | { kind: "refused_program"; programs: string[]; command: string; agent: string;
      agent_has_own_list?: boolean }
  // The browser toolset.
  | { kind: "page"; page: string; url: string; frames: number; asked_frames: number;
      needs_build: boolean; errors: PageErrors; canvas: boolean; size: string;
      blank: boolean | null }
  | { kind: "canvas"; canvas: boolean; canvases: number; size: string;
      blank: boolean | null; moving: boolean | null; frames: number;
      note: string | null; errors: PageErrors }
  | { kind: "key"; key: string; times: number; delivered: boolean;
      changed: boolean | null; frames: number; diff?: ImageDiff | null;
      shot_before?: string; shot_after?: string }
  | { kind: "wait"; frames: number; asked_frames: number; changed: boolean | null;
      stalled: boolean; errors: PageErrors; diff?: ImageDiff | null;
      shot_before?: string; shot_after?: string }
  | { kind: "screenshot"; shot: string; question: string; checks: string[];
      answer: string; prompt?: string; model?: string; preset?: string;
      usage?: CallUsage; clipped?: boolean; page?: string; url?: string;
      region?: { x: number; y: number; width: number; height: number };
      error?: string }
  | { kind: "click"; text: string; x?: number | null; y?: number | null;
      label?: string; delivered?: boolean; changed?: boolean | null; frames: number;
      diff?: ImageDiff | null; shot_before?: string; shot_after?: string }
  | { kind: "mouse"; dx: number; dy: number; delivered?: boolean; locked?: boolean;
      movement?: [number, number]; changed?: boolean | null; frames: number;
      diff?: ImageDiff | null; shot_before?: string; shot_after?: string }
  | { kind: "film"; shots: string[]; question: string; checks: string[];
      answer: string; frames: number; frames_between: number;
      /** Fraction of the picture that changed between consecutive frames. */
      motion: number[]; moving: boolean; prompt?: string; model?: string;
      preset?: string; usage?: CallUsage; error?: string }
  | { kind: "page_failed" | "canvas_failed" | "key_failed" | "look_failed"
        | "wait_failed" | "watch_failed" | "click_failed" | "mouse_failed"; error: string;
      text?: string; candidates?: string[] };

export interface ImageDiff {
  identical: boolean;
  differing: number | null;
  total: number | null;
  fraction: number;
  how: "pixels" | "bytes";
  note: string;
  described: string;
}

export interface PageErrors {
  console: string[];
  exceptions: string[];
  failed_requests: string[];
  total: number;
}

export interface FileStat {
  path: string;
  exists?: boolean;
  is_dir?: boolean;
  blank?: boolean;
  size_bytes?: number;
  lines?: number;
  error?: string;
}

/** Token counts on a single model call, as the provider reported them. */
export interface CallUsage {
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
}

export interface EventPayload {
  [key: string]: unknown;
  name?: string;
  arguments?: Record<string, unknown>;
  ok?: boolean;
  result?: string;
  result_tokens?: number;
  remit_violation?: string | null;
  detail?: ToolDetail;
  /** model_call */
  round?: number;
  model?: string;
  preset?: string;
  base_url?: string;
  duration_ms?: number;
  /** The prompt, or — for an event whose big fields were not kept on disk — a
   *  string saying so. Reading this as an array crashed the console: it is one
   *  or the other, and only the server knows which. */
  messages?: ChatMessage[] | string;
  /** Fields the event carries as a note rather than a value. */
  truncated_on_disk?: string[];
  response_text?: string;
  reasoning?: string;
  tool_calls?: { name: string; arguments: Record<string, unknown> }[];
  finish_reason?: string;
  /** True when the call went out with thinking on. Absent on backends whose
   *  thinking trance does not set — an unset toggle is not an off one. */
  thinking?: boolean;
  /** The round happened because the tool rounds ran out. */
  out_of_rounds?: boolean;
  usage?: CallUsage;
  summary?: { message_count?: number; est_tokens?: number };
  /** command_started / command_finished: the command and the handle that
   *  cancels it while it is still going. */
  command?: string;
  command_id?: string;
  /** model_waiting: the gauge, sized before the answer exists */
  context?: ContextUsage;
  /** context_compacted: the checkpoint summary that replaced old rounds. */
  checkpoint?: string;
  /** model_progress: one live frame of a streaming generation. Transient —
   *  on the socket only, never in fetched history. */
  phase?: "thinking" | "answering";
  tokens?: number;
  elapsed_s?: number;
  tail?: string;
  /** What the reply is measured against: the token cap, and the time budget
   *  when the preset cuts by time (0 when it cuts by size). */
  cap_tokens?: number;
  cap_seconds?: number;
  /** what /events drops to keep the payload small */
  _omitted?: Record<string, number>;
}

export interface TranceEvent {
  id: string;
  type: string;
  session_id: string;
  ts: string;
  agent?: string;
  step_id?: string;
  payload: EventPayload;
}

// ---------------------------------------------------------------- config

export interface Planning {
  max_step_points: number;
  scale: number[];
  escalation_preset: string;
  escalation_role: string;
  git_commits: boolean;
  git_auto_init: boolean;
  /** An agent whose step opens every generated plan. Empty = none. */
  plan_open?: string;
  /** An agent or loop appended when a plan does not already end with it. */
  plan_close?: string;
}

export interface VisualSupport {
  browser: boolean;
}

export interface AppConfig {
  config: Record<string, unknown>;
  presets: ModelPreset[];
  kinds: Record<string, KindDefault>;
  /** The point scale the orchestrator estimates against. Run settings are not
   *  here: they belong to a project, and this endpoint does not know which. */
  scale: number[];
  visual: VisualSupport;
  orchestrator: ResolvedModel & { preset?: string };
  /** True when the source on disk is newer than the running process. */
  stale: boolean;
}

// ----------------------------------------------------------------- loops

export interface LoopEdge {
  target: string;
  max_visits?: number;
  /** Take this route on the agent's backup model rather than its usual one. */
  backup?: boolean;
}

export interface LoopNode {
  id: string;
  role: string;
  focus: string;
  /** Legacy single check; `checks` is the chain that runs. */
  check: string | null;
  /** The same chips a step carries: copied once from the node's agent, then
   *  the loop's own to edit. */
  checks?: string[];
  checks_seeded?: boolean;
  revert_on_fail?: boolean;
  on: Record<string, LoopEdge[]>;
}

export interface Loop {
  name: string;
  description: string;
  prompt: string;
  nodes: LoopNode[];
  start: string;
  max_steps: number;
  roles?: string[];
}

// -------------------------------------------------------------- commands

export interface CommandPolicy {
  allowed: string[];
  shell: boolean;
}

export interface CommandLists {
  /** The effective allowlist — the default list, flattened. */
  allowed: string[];
  shell: boolean;
  names: string[];
  lists: Record<string, CommandPolicy>;
  /** Programs trance ships with, so the UI can show what is an addition. */
  defaults?: string[];
  /** Which agents name their own list rather than using the default. */
  overrides?: Record<string, string>;
  agents_with_commands?: string[];
}

// ----------------------------------------------------------------- files

/** One file, as the server lists it. The list is FLAT — there is no tree from
 *  the server, and the UI builds one from these paths. */
export interface ProjectFile {
  path: string;
  bytes: number;
  lines: number;
}

/** Per-extension rollup, which is where the file and line counts come from. */
export interface ExtensionTotal {
  ext: string;
  files: number;
  lines: number;
  bytes: number;
}

export interface FileListing {
  root: string;
  files: ProjectFile[];
  totals: ExtensionTotal[];
}

/** A folder in the tree the UI builds. Not a server shape. */
export interface TreeNode {
  name: string;
  path: string;
  file?: ProjectFile;
  children: TreeNode[];
}

/** All fields present but empty when nothing is being served, rather than the
 *  endpoint answering null. */
export interface Preview {
  root: string;
  port: number;
  url: string;
  /** The public tunnel address, when one is running. */
  public: string;
  /** Set when the project's own dev server is running rather than a static
   *  server over its files. */
  dev?: boolean;
  command?: string;
  /** Where the page is opened from — the host this browser reached trance on. */
  open?: string;
  /** Set when an iteration's version is being served rather than the tree. */
  version?: string;
  of_message?: string;
  /** What still has to be done by hand, if anything. Currently the one thing:
   *  a Vite dev server refuses a tunnel until its host is allowed. */
  hint?: string;
  needs_build?: boolean;
  build_command?: string;
}

/** The orchestrator's answer to "how is this project started". */
export interface RunPlan {
  command: string;
  dir: string;
  why: string;
  static_instead: boolean;
  read_readme: boolean;
}

export interface ModelSpend {
  model: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  /** Of the input, cache re-reads — billed at roughly a tenth of a fresh
   *  token. Claude Code re-reads its whole conversation every internal turn,
   *  so without this split its raw input count reads 20x every other backend
   *  while most of it is the same tokens over and over. */
  cache_read_tokens?: number;
  /** Generation rate over the calls that reported a duration — what the
   *  machine sustains end to end, prompt processing included. 0 = untimed. */
  tokens_per_second?: number;
  total: number;
}

export interface Usage {
  models: ModelSpend[];
  total: number;
  calls: number;
}

export interface ReviewChanges {
  review: unknown | null;
  files: string[];
  diff: string;
}
