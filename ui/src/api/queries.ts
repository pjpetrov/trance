/** Server state, owned by TanStack Query.
 *
 * Nothing in this app keeps a copy of what the server said. Every screen asks
 * for what it needs and gets the cached answer; every mutation invalidates the
 * keys it invalidated. That is the whole reason for the rewrite — the old UI
 * held one global `state` object, wrote into it from the socket, from forty
 * fetch callbacks, and from the flow editor, and the bugs were all the same
 * bug: two of those three disagreeing about what was true.
 *
 * The websocket does not write into components. It writes into this cache
 * (see hooks/useSessionSocket), so a live update and a refetch land in exactly
 * the same place.
 */

import { useQuery, type UseQueryOptions } from "@tanstack/react-query";
import { api } from "./client";
import type {
  AgentRole, AppConfig, CommandLists, Loop, ModelPreset, Session, TranceEvent, Usage,
} from "./types";

/** Every key in one place. A key built inline at the call site is a cache miss
 *  waiting to happen, and an invalidate that silently matches nothing. */
export const keys = {
  config: ["config"] as const,
  workspace: ["workspace"] as const,
  agents: ["agents"] as const,
  presets: ["presets"] as const,
  loops: ["loops"] as const,
  commands: ["commands"] as const,

  sessions: ["sessions"] as const,
  session: (id: string) => ["session", id] as const,
  events: (id: string) => ["events", id] as const,
  stepEvents: (id: string, stepId: string) => ["events", id, "step", stepId] as const,
  usage: (id: string) => ["usage", id] as const,
  memory: (id: string) => ["memory", id] as const,
  files: (id: string) => ["files", id] as const,
  file: (id: string, path: string) => ["file", id, path] as const,
  preview: (id: string) => ["preview", id] as const,
  reviews: (id: string) => ["reviews", id] as const,
  reviewChanges: (id: string) => ["review-changes", id] as const,
  commit: (id: string, sha: string) => ["commit", id, sha] as const,
};

/** Configuration changes when the user changes it, not on its own. Refetching
 *  it on every window focus costs a round trip to say nothing. */
const CONFIG_STALE = 5 * 60_000;

export function useConfig(options?: Partial<UseQueryOptions<AppConfig>>) {
  return useQuery({
    queryKey: keys.config,
    queryFn: api.config,
    staleTime: CONFIG_STALE,
    ...options,
  });
}

export function useAgents() {
  return useQuery({
    queryKey: keys.agents,
    queryFn: async () => (await api.agents()),
    staleTime: CONFIG_STALE,
    select: (data) => ({
      ...data,
      byName: Object.fromEntries(data.agents.map((role) => [role.name, role])) as
        Record<string, AgentRole>,
    }),
  });
}

export function usePresets() {
  return useQuery({
    queryKey: keys.presets,
    queryFn: async () => (await api.presets()).presets,
    staleTime: CONFIG_STALE,
  });
}

export function useLoops() {
  return useQuery({
    queryKey: keys.loops,
    queryFn: async () => (await api.loops()).loops,
    staleTime: CONFIG_STALE,
  });
}

export function useCommands() {
  return useQuery<CommandLists>({
    queryKey: keys.commands,
    queryFn: api.commands,
    staleTime: CONFIG_STALE,
  });
}

// -------------------------------------------------------------- sessions

export function useSessions() {
  return useQuery({
    queryKey: keys.sessions,
    queryFn: api.sessions,
    // The list shows each session's status, and a run started in another tab
    // should not leave this one saying "ready" indefinitely.
    refetchInterval: 15_000,
  });
}

export function useSession(sessionId: string | null) {
  return useQuery<Session>({
    queryKey: keys.session(sessionId ?? ""),
    queryFn: () => api.session(sessionId!),
    enabled: Boolean(sessionId),
    // The socket pushes a fresh snapshot after anything that changes the flow,
    // so polling would only duplicate what is already arriving. When the socket
    // is down, useSessionSocket turns polling back on.
    staleTime: Infinity,
  });
}

/** The console's backlog. Deliberately a bounded tail, not the run: a finished
 *  session is thousands of events and tens of megabytes, nearly all of it
 *  prompts that no panel ever shows. */
export function useEventTail(sessionId: string | null) {
  return useQuery<TranceEvent[]>({
    queryKey: keys.events(sessionId ?? ""),
    queryFn: async () => (await api.eventTail(sessionId!)).events,
    enabled: Boolean(sessionId),
    staleTime: Infinity,            // the socket appends; refetching would duplicate
  });
}

/** One step's own calls, fetched when you open it rather than held for every
 *  step of the run. This is the request that used to be a 13MB page load. */
export function useStepEvents(sessionId: string | null, stepId: string | null) {
  return useQuery<TranceEvent[]>({
    queryKey: keys.stepEvents(sessionId ?? "", stepId ?? ""),
    queryFn: () => api.stepEvents(sessionId!, stepId!),
    enabled: Boolean(sessionId && stepId),
    staleTime: 10_000,
  });
}

/** One event in full, fetched only when something wants what /events drops.
 *
 *  The list endpoint strips the prompt, the reasoning and the raw reply — a
 *  step of a long run is 13MB and three quarters of it is `messages`. So the
 *  console shows the slim version and asks for the rest when a line is opened,
 *  which is the only time anyone reads it. */
export function useFullEvent(sessionId: string | null, eventId: string | null) {
  return useQuery<TranceEvent>({
    queryKey: ["event", sessionId ?? "", eventId ?? ""],
    queryFn: () => api.event(sessionId!, eventId!),
    enabled: Boolean(sessionId && eventId),
    staleTime: Infinity,            // an event that has happened cannot change
  });
}

export function useUsage(sessionId: string | null) {
  return useQuery<Usage>({
    queryKey: keys.usage(sessionId ?? ""),
    queryFn: () => api.usage(sessionId!),
    enabled: Boolean(sessionId),
    refetchInterval: 10_000,
  });
}

export function useMemory(sessionId: string | null, enabled = true) {
  return useQuery({
    queryKey: keys.memory(sessionId ?? ""),
    queryFn: () => api.memory(sessionId!),
    enabled: Boolean(sessionId) && enabled,
  });
}

export function useFiles(sessionId: string | null, enabled = true) {
  return useQuery({
    queryKey: keys.files(sessionId ?? ""),
    queryFn: () => api.files(sessionId!),
    enabled: Boolean(sessionId) && enabled,
  });
}

export function useFile(sessionId: string | null, path: string | null) {
  return useQuery({
    queryKey: keys.file(sessionId ?? "", path ?? ""),
    queryFn: () => api.file(sessionId!, path!),
    enabled: Boolean(sessionId && path),
  });
}

export function usePreview(sessionId: string | null) {
  return useQuery({
    queryKey: keys.preview(sessionId ?? ""),
    queryFn: () => api.preview(sessionId!),
    enabled: Boolean(sessionId),
  });
}

export function useReviews(sessionId: string | null, enabled = true) {
  return useQuery({
    queryKey: keys.reviews(sessionId ?? ""),
    queryFn: async () => (await api.reviews(sessionId!)).reviews,
    enabled: Boolean(sessionId) && enabled,
  });
}

export function useCommit(sessionId: string | null, sha: string | null) {
  return useQuery({
    queryKey: keys.commit(sessionId ?? "", sha ?? ""),
    queryFn: () => api.commit(sessionId!, sha!),
    enabled: Boolean(sessionId && sha),
    // A commit is immutable. Fetch it once and keep it for the session.
    staleTime: Infinity,
  });
}

export type { AgentRole, Loop, ModelPreset, Session, TranceEvent };
