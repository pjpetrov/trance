/** One way to reach the server.
 *
 * The old UI had a single `api()` helper and then forty call sites that each
 * built their own URL by string concatenation, which is how a session id with a
 * slash in it would have broken half of them. Every path is a function here,
 * every id goes through encodeURIComponent, and the return type is the thing
 * the caller actually gets.
 */

import type {
  AgentRole, Approval, AppConfig, CommandLists, FileListing, Flow, Loop, ModelPreset,
  MessageCommits, Planning, Preview, ReviewChanges, ReviewComment, Session, Step,
  TranceEvent, Usage,
} from "./types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
    readonly path: string,
  ) {
    // The status is part of the message because the UI shows these verbatim in
    // a toast, and "409" versus "500" is the difference between "you already
    // have one of those" and "something is broken".
    super(`${status}: ${detail}`);
    this.name = "ApiError";
  }
}

type Options = {
  method?: "GET" | "POST" | "PUT" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
};

async function request<T>(path: string, options: Options = {}): Promise<T> {
  const { method = "GET", body, signal } = options;
  const response = await fetch(path, {
    method,
    signal,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  if (!response.ok) {
    // FastAPI puts the message in `detail`; anything else that fails (a proxy,
    // a crash before the app) sends text, and swallowing that leaves the user
    // staring at a bare status code.
    let detail = response.statusText;
    const text = await response.text().catch(() => "");
    if (text) {
      try {
        const parsed = JSON.parse(text) as { detail?: unknown };
        detail = typeof parsed.detail === "string" ? parsed.detail : text;
      } catch {
        detail = text;
      }
    }
    throw new ApiError(response.status, detail, path);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const id = encodeURIComponent;

/** Agents, loops, allowlists and settings belong to a project now, so the calls
 *  that read or change them have to say which. Required rather than defaulted:
 *  guessing would silently edit another project's agents. */
const forProject = (sid: string, extra = "") =>
  `?session=${id(sid)}${extra ? `&${extra}` : ""}`;

export const api = {
  config: () => request<AppConfig>("/api/config"),
  workspace: () => request<{
    workspace: string; state_dir: string; writable: boolean;
    suggested_name: string; suggested_dir: string;
  }>("/api/workspace"),
  // No checkPath: nothing types a path any more, and the stub that was here
  // sent {path} to an endpoint that reads project_dir — it would have failed
  // the first time anyone called it.

  settings: (sid: string) =>
    request<Planning & { scale: number[]; migrated: boolean }>(
      `/api/sessions/${id(sid)}/settings`),
  setPlanning: (sid: string, body: Partial<Planning>) =>
    request<Planning>(`/api/config/planning${forProject(sid)}`, { method: "PUT", body }),
  setOrchestrator: (body: { preset?: string }) =>
    request<{ preset: string; provider: string; model: string; base_url: string }>(
      "/api/config/orchestrator", { method: "PUT", body }),

  // ------------------------------------------------------------- agents
  agents: (sid: string) =>
    request<{ agents: AgentRole[]; verifiers: string[]; toolsets: string[] }>(
      `/api/agents${forProject(sid)}`),
  saveAgent: (sid: string, name: string, body: Partial<AgentRole>) =>
    request<AgentRole>(`/api/agents/${id(name)}${forProject(sid)}`,
      { method: "PUT", body }),
  deleteAgent: (sid: string, name: string) =>
    request<void>(`/api/agents/${id(name)}${forProject(sid)}`, { method: "DELETE" }),
  resetAgent: (sid: string, name: string) =>
    request<AgentRole>(`/api/agents/${id(name)}/reset${forProject(sid)}`,
      { method: "POST" }),
  draftPrompt: (body: { name: string; title?: string; description?: string }) =>
    request<{ prompt: string; description?: string }>("/api/agents/draft-prompt",
      { method: "POST", body }),

  // ------------------------------------------------------------- models
  presets: () => request<{ presets: ModelPreset[] }>("/api/presets"),
  savePreset: (name: string, body: Partial<ModelPreset>) =>
    request<ModelPreset>(`/api/presets/${id(name)}`, { method: "PUT", body }),
  deletePreset: (name: string) =>
    request<void>(`/api/presets/${id(name)}`, { method: "DELETE" }),
  renamePreset: (name: string, to: string) =>
    request<ModelPreset>(`/api/presets/${id(name)}/rename`, { method: "POST", body: { to } }),
  checkPreset: (name: string) =>
    request<{ ok: boolean; error?: string; model?: string; took_ms?: number }>(
      `/api/presets/${id(name)}/check`, { method: "POST" }),
  discoverModels: (body: { kind: string; base_url: string; api_key?: string | null }) =>
    request<{ models: string[]; error?: string }>("/api/models/discover",
      { method: "POST", body }),

  // -------------------------------------------------------------- loops
  loops: (sid: string) => request<{ loops: Loop[] }>(`/api/loops${forProject(sid)}`),
  saveLoop: (sid: string, name: string, body: Partial<Loop>) =>
    request<Loop>(`/api/loops/${id(name)}${forProject(sid)}`, { method: "PUT", body }),
  deleteLoop: (sid: string, name: string) =>
    request<void>(`/api/loops/${id(name)}${forProject(sid)}`, { method: "DELETE" }),

  // ----------------------------------------------------------- commands
  commands: (sid: string) => request<CommandLists>(`/api/commands${forProject(sid)}`),
  saveCommandList: (sid: string, name: string,
                    body: { allowed?: string[]; shell?: boolean }) =>
    request<CommandLists>(`/api/commands${forProject(sid)}`,
      { method: "PUT", body: { ...body, name } }),
  deleteCommandList: (sid: string, name: string) =>
    request<CommandLists>(`/api/commands/${id(name)}${forProject(sid)}`,
      { method: "DELETE" }),
  allowProgram: (sid: string, body: { programs: string[]; agent?: string }) =>
    request<CommandLists>(`/api/commands/allow${forProject(sid)}`,
      { method: "POST", body }),
  resetCommands: (sid: string) =>
    request<CommandLists>(`/api/commands/reset${forProject(sid)}`, { method: "POST" }),
  cancelCommand: (commandId: string) =>
    request<{ cancelled: boolean }>(`/api/commands/cancel/${id(commandId)}`,
      { method: "POST" }),

  // ----------------------------------------------------------- sessions
  sessions: () => request<Session[]>("/api/sessions"),
  session: (sid: string) => request<Session>(`/api/sessions/${id(sid)}`),
  createSession: (body: { name: string; project_dir?: string; goal?: string }) =>
    request<Session>("/api/sessions", { method: "POST", body }),
  deleteSession: (sid: string, files = false) =>
    request<{ deleted: string; project_dir: string; files_deleted: boolean }>(
      `/api/sessions/${id(sid)}${files ? "?files=true" : ""}`, { method: "DELETE" }),

  chat: (sid: string, body: { message: string; images?: string[] }) =>
    request<{ session: Session; reply?: string }>(`/api/sessions/${id(sid)}/chat`,
      { method: "POST", body }),
  saveFlow: (sid: string, body: { steps: Partial<Step>[] }) =>
    request<{ steps: Step[]; team: AgentRole[] }>(`/api/sessions/${id(sid)}/flow`,
      { method: "PUT", body }),
  flow: (sid: string) => request<Flow>(`/api/sessions/${id(sid)}/flow`),

  start: (sid: string) => request<Session>(`/api/sessions/${id(sid)}/start`, { method: "POST" }),
  pause: (sid: string) => request<Session>(`/api/sessions/${id(sid)}/pause`, { method: "POST" }),
  resume: (sid: string) => request<Session>(`/api/sessions/${id(sid)}/resume`, { method: "POST" }),
  stop: (sid: string) => request<Session>(`/api/sessions/${id(sid)}/stop`, { method: "POST" }),
  steer: (sid: string, body: { note: string; step_id?: string }) =>
    request<{ delivered: boolean; note: string }>(`/api/sessions/${id(sid)}/steer`,
      { method: "POST", body }),

  splitStep: (sid: string, stepId: string) =>
    request<{ steps: Step[] }>(`/api/sessions/${id(sid)}/steps/${id(stepId)}/split`,
      { method: "POST" }),
  rerunStep: (sid: string, stepId: string, body?: { from?: string }) =>
    request<Session>(`/api/sessions/${id(sid)}/steps/${id(stepId)}/rerun`,
      { method: "POST", body: body ?? {} }),
  skipStep: (sid: string, stepId: string) =>
    request<Session>(`/api/sessions/${id(sid)}/steps/${id(stepId)}/skip`, { method: "POST" }),

  approvals: (sid: string) =>
    request<{ pending: Approval[]; enabled: boolean; timeout_s: number }>(
      `/api/sessions/${id(sid)}/approvals`),
  resolveApproval: (sid: string, requestId: string, decision: "once" | "always" | "deny") =>
    request<Approval & { widened: boolean }>(
      `/api/sessions/${id(sid)}/approvals/${id(requestId)}`,
      { method: "POST", body: { decision } }),

  // ------------------------------------------------------------- events
  /** The console's own tail. Never the whole run: a finished session is tens of
   *  megabytes and almost all of it is prompts nobody opens. */
  eventTail: (sid: string, limit?: number) =>
    request<{ events: TranceEvent[]; total: number; shown: number }>(
      `/api/sessions/${id(sid)}/events?tail=true${limit ? `&limit=${limit}` : ""}`),
  stepEvents: (sid: string, stepId: string) =>
    request<TranceEvent[]>(`/api/sessions/${id(sid)}/events?step=${id(stepId)}`),
  event: (sid: string, eventId: string) =>
    request<TranceEvent>(`/api/sessions/${id(sid)}/events/${id(eventId)}`),

  usage: (sid: string) => request<Usage>(`/api/sessions/${id(sid)}/usage`),
  /** Every session's spend, including models whose preset has since gone. */
  lifetimeUsage: () => request<Usage>("/api/usage"),

  // ------------------------------------------------------------- memory
  memory: (sid: string) =>
    request<{ path: string; notes: string[]; raw: string; prompt_view: string;
              oversized: boolean; max_notes: number }>(`/api/sessions/${id(sid)}/memory`),
  saveMemory: (sid: string, body: { raw: string }) =>
    request<unknown>(`/api/sessions/${id(sid)}/memory`, { method: "PUT", body }),
  compactMemory: (sid: string) =>
    request<{ compacted: boolean; before?: number; after?: number; reason?: string }>(
      `/api/sessions/${id(sid)}/memory/compact`, { method: "POST" }),

  // -------------------------------------------------------------- files
  files: (sid: string) => request<FileListing>(`/api/sessions/${id(sid)}/files`),
  file: (sid: string, path: string) =>
    request<{ path: string; content: string; bytes: number; lines: number }>(
      `/api/sessions/${id(sid)}/file?path=${id(path)}`),
  deleteFile: (sid: string, path: string) =>
    request<{ deleted: string; committed: boolean }>(
      `/api/sessions/${id(sid)}/file?path=${id(path)}`, { method: "DELETE" }),
  writeFile: (sid: string, body: { path: string; content: string }) =>
    request<{ path: string; bytes: number; committed: boolean }>(
      `/api/sessions/${id(sid)}/file`, { method: "PUT", body }),

  // ------------------------------------------------------------ preview
  startPreview: (sid: string, body: { path?: string }) =>
    request<Preview & { dev?: unknown; bare?: unknown[] }>(
      `/api/sessions/${id(sid)}/preview`, { method: "POST", body }),
  preview: (sid: string) => request<Preview>(`/api/sessions/${id(sid)}/preview`),
  stopPreview: (sid: string) =>
    request<{ stopped: boolean }>(`/api/sessions/${id(sid)}/preview`, { method: "DELETE" }),
  share: (sid: string, body: { stop?: boolean }) =>
    request<{ url?: string; stopped?: boolean }>(`/api/sessions/${id(sid)}/share`,
      { method: "POST", body }),

  // ------------------------------------------------------------- review
  review: (sid: string, body: ReviewBody) =>
    request<ReviewComment>(`/api/sessions/${id(sid)}/review`, { method: "POST", body }),
  dropReview: (sid: string, noteId: string) =>
    request<{ deleted: string; left: number }>(
      `/api/sessions/${id(sid)}/review/${id(noteId)}`, { method: "DELETE" }),
  finishReview: (sid: string, loop?: string) =>
    request<FinishedReview>(`/api/sessions/${id(sid)}/review/finish`,
      { method: "POST", body: { loop: loop || undefined } }),
  reviews: (sid: string) =>
    request<{ reviews: ReviewRound[] }>(`/api/sessions/${id(sid)}/reviews`),
  reviewChanges: (sid: string) =>
    request<ReviewChanges>(`/api/sessions/${id(sid)}/review/changes`),
  messageCommits: (sid: string, messageId: string) =>
    request<MessageCommits>(
      `/api/sessions/${id(sid)}/messages/${id(messageId)}/commits`),
  commit: (sid: string, sha: string) =>
    request<Commit & { stat: string; diff: string; clipped: boolean }>(
      `/api/sessions/${id(sid)}/commit/${id(sha)}`),

  /** A screenshot a visual step took, served from the project's .trance/shots. */
  shotUrl: (sid: string, shot: string) => `/api/sessions/${id(sid)}/shot/${shot}`,
};

export interface ReviewBody {
  path?: string;
  line?: number | null;
  note: string;
}

export interface Commit {
  sha: string;
  short: string;
  subject: string;
  when: string;
  who: string;
}

/** What POST /review/finish answers with: the record it made, plus the step it
 *  put on the flow. Not a session — sending it through the session cache is how
 *  the flow ended up replaced by an object that was never one. */
export interface FinishedReview {
  id: string;
  step_id: string;
  notes: { path?: string; line?: number | null; note: string }[];
  before: string;
  at: string;
  started: boolean;
  flow: { steps: unknown[]; cursor: number };
}

export interface ReviewRound {
  review: string;
  at: string;
  status: string;
  notes: { path?: string; line?: number | null; note: string }[];
  before: string;
  after: string;
  files: string[];
  commits: Commit[];
}
