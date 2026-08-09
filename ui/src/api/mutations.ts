/** Everything that changes something, with what it invalidates.
 *
 * The pairing is the point. In the old UI each write was a fetch followed by
 * whichever repaint calls the author remembered, so editing an agent updated
 * the agents modal but not the model picker on the step next to it. Here the
 * keys a write affects are written down beside the write.
 *
 * Session-shaped responses are set straight into the cache rather than
 * invalidated: the server returns the new session, so refetching it would ask
 * for something we are already holding.
 */

import { useMutation, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api, type ReviewBody } from "./client";
import { keys } from "./queries";
import type { AgentRole, Loop, ModelPreset, Planning, Session, Step } from "./types";

function putSession(client: QueryClient, session: Session) {
  client.setQueryData(keys.session(session.id), session);
  // The session list shows status and progress; a run that just started or
  // finished changes both.
  void client.invalidateQueries({ queryKey: keys.sessions });
}

// ------------------------------------------------------------- the session

export function useStartRun(sessionId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.start(sessionId),
    onSuccess: (session) => putSession(client, session),
  });
}

export function useRunControl(sessionId: string) {
  const client = useQueryClient();
  const settle = (session: Session) => putSession(client, session);
  return {
    pause: useMutation({ mutationFn: () => api.pause(sessionId), onSuccess: settle }),
    resume: useMutation({ mutationFn: () => api.resume(sessionId), onSuccess: settle }),
    stop: useMutation({ mutationFn: () => api.stop(sessionId), onSuccess: settle }),
  };
}

export function useSteer(sessionId: string) {
  return useMutation({
    mutationFn: (body: { note: string; step_id?: string }) => api.steer(sessionId, body),
  });
}

export function useChat(sessionId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ message, images }: { message: string; images?: string[] }) =>
      api.chat(sessionId, { message, images }),
    onSuccess: (result) => {
      if (result.session) putSession(client, result.session);
    },
  });
}

export function useSaveFlow(sessionId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (steps: Partial<Step>[]) => api.saveFlow(sessionId, { steps }),
    // The flow save returns steps and the team, not a whole session — so this
    // one genuinely has to refetch.
    onSuccess: () => client.invalidateQueries({ queryKey: keys.session(sessionId) }),
  });
}

export function useStepActions(sessionId: string) {
  const client = useQueryClient();
  const settle = (session: Session) => putSession(client, session);
  return {
    rerun: useMutation({
      mutationFn: (stepId: string) => api.rerunStep(sessionId, stepId),
      onSuccess: settle,
    }),
    skip: useMutation({
      mutationFn: (stepId: string) => api.skipStep(sessionId, stepId),
      onSuccess: settle,
    }),
    split: useMutation({
      mutationFn: (stepId: string) => api.splitStep(sessionId, stepId),
      onSuccess: () => client.invalidateQueries({ queryKey: keys.session(sessionId) }),
    }),
  };
}

export function useSessionLifecycle() {
  const client = useQueryClient();
  return {
    create: useMutation({
      mutationFn: (body: { name: string; project_dir: string; goal?: string }) =>
        api.createSession(body),
      onSuccess: (session) => putSession(client, session),
    }),
    remove: useMutation({
      mutationFn: (sessionId: string) => api.deleteSession(sessionId),
      onSuccess: (_result, sessionId) => {
        client.removeQueries({ queryKey: keys.session(sessionId) });
        client.removeQueries({ queryKey: keys.events(sessionId) });
        void client.invalidateQueries({ queryKey: keys.sessions });
      },
    }),
  };
}

// ------------------------------------------------------------------ agents

export function useAgentMutations() {
  const client = useQueryClient();
  // An agent's remit and model show up on the plan screen and inside every
  // session's team, so both go stale together.
  const settle = () => {
    void client.invalidateQueries({ queryKey: keys.agents });
    void client.invalidateQueries({ queryKey: keys.config });
    void client.invalidateQueries({ queryKey: keys.sessions });
  };
  return {
    save: useMutation({
      mutationFn: ({ name, body }: { name: string; body: Partial<AgentRole> }) =>
        api.saveAgent(name, body),
      onSuccess: settle,
    }),
    remove: useMutation({ mutationFn: api.deleteAgent, onSuccess: settle }),
    reset: useMutation({ mutationFn: api.resetAgent, onSuccess: settle }),
    draftPrompt: useMutation({ mutationFn: api.draftPrompt }),
  };
}

// ------------------------------------------------------------------ models

export function usePresetMutations() {
  const client = useQueryClient();
  const settle = () => {
    void client.invalidateQueries({ queryKey: keys.presets });
    // Agents name a preset; renaming or deleting one changes what they resolve
    // to, and the resolved model is shown next to every agent.
    void client.invalidateQueries({ queryKey: keys.agents });
    void client.invalidateQueries({ queryKey: keys.config });
  };
  return {
    save: useMutation({
      mutationFn: ({ name, body }: { name: string; body: Partial<ModelPreset> }) =>
        api.savePreset(name, body),
      onSuccess: settle,
    }),
    remove: useMutation({ mutationFn: api.deletePreset, onSuccess: settle }),
    rename: useMutation({
      mutationFn: ({ name, to }: { name: string; to: string }) => api.renamePreset(name, to),
      onSuccess: settle,
    }),
    check: useMutation({ mutationFn: api.checkPreset }),
    discover: useMutation({ mutationFn: api.discoverModels }),
  };
}

// ------------------------------------------------------------------- loops

export function useLoopMutations() {
  const client = useQueryClient();
  const settle = () => void client.invalidateQueries({ queryKey: keys.loops });
  return {
    save: useMutation({
      mutationFn: ({ name, body }: { name: string; body: Partial<Loop> }) =>
        api.saveLoop(name, body),
      onSuccess: settle,
    }),
    remove: useMutation({ mutationFn: api.deleteLoop, onSuccess: settle }),
  };
}

// ---------------------------------------------------------------- commands

export function useCommandMutations() {
  const client = useQueryClient();
  const settle = () => void client.invalidateQueries({ queryKey: keys.commands });
  return {
    save: useMutation({
      mutationFn: ({ name, body }: { name: string; body: { allowed?: string[]; shell?: boolean } }) =>
        api.saveCommandList(name, body),
      onSuccess: settle,
    }),
    remove: useMutation({ mutationFn: api.deleteCommandList, onSuccess: settle }),
    allow: useMutation({ mutationFn: api.allowProgram, onSuccess: settle }),
    reset: useMutation({ mutationFn: api.resetCommands, onSuccess: settle }),
    cancel: useMutation({ mutationFn: api.cancelCommand }),
  };
}

// -------------------------------------------------------------- settings

export function useSettingsMutations() {
  const client = useQueryClient();
  return {
    planning: useMutation({
      mutationFn: (body: Partial<Planning>) => api.setPlanning(body),
      onSuccess: () => client.invalidateQueries({ queryKey: keys.config }),
    }),
    orchestrator: useMutation({
      mutationFn: (body: { preset?: string }) => api.setOrchestrator(body),
      onSuccess: () => client.invalidateQueries({ queryKey: keys.config }),
    }),
  };
}

// ------------------------------------------------------- memory and files

export function useMemoryMutations(sessionId: string) {
  const client = useQueryClient();
  const settle = () => void client.invalidateQueries({ queryKey: keys.memory(sessionId) });
  return {
    save: useMutation({
      mutationFn: (raw: string) => api.saveMemory(sessionId, { raw }),
      onSuccess: settle,
    }),
    compact: useMutation({ mutationFn: () => api.compactMemory(sessionId), onSuccess: settle }),
  };
}

export function useWriteFile(sessionId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { path: string; content: string }) => api.writeFile(sessionId, body),
    onSuccess: (_result, body) => {
      void client.invalidateQueries({ queryKey: keys.file(sessionId, body.path) });
      void client.invalidateQueries({ queryKey: keys.files(sessionId) });
    },
  });
}

// ------------------------------------------------------- review + preview

export function useReviewMutations(sessionId: string) {
  const client = useQueryClient();
  const reload = () => {
    void client.invalidateQueries({ queryKey: keys.session(sessionId) });
  };
  return {
    // Both answer with the note, not the session, so the session is refetched
    // rather than replaced by a shape that was never one.
    add: useMutation({
      mutationFn: (body: ReviewBody) => api.review(sessionId, body),
      onSuccess: reload,
    }),
    drop: useMutation({
      mutationFn: (noteId: string) => api.dropReview(sessionId, noteId),
      onSuccess: reload,
    }),
    finish: useMutation({
      mutationFn: (loop?: string) => api.finishReview(sessionId, loop),
      onSuccess: () => {
        // It answers with the review record and the new flow, not a session, so
        // the session is refetched rather than replaced with the wrong shape.
        void client.invalidateQueries({ queryKey: keys.session(sessionId) });
        void client.invalidateQueries({ queryKey: keys.reviews(sessionId) });
      },
    }),
  };
}

export function usePreviewMutations(sessionId: string) {
  const client = useQueryClient();
  const settle = () => void client.invalidateQueries({ queryKey: keys.preview(sessionId) });
  return {
    start: useMutation({
      mutationFn: (path?: string) => api.startPreview(sessionId, { path }),
      onSuccess: settle,
    }),
    share: useMutation({
      mutationFn: (stop?: boolean) => api.share(sessionId, { stop }),
      onSuccess: settle,
    }),
    // Stopping takes the tunnel with it: a public link to a server that is gone
    // is a link that answers 502.
    stop: useMutation({ mutationFn: () => api.stopPreview(sessionId), onSuccess: settle }),
  };
}
