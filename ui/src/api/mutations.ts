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

/** Answering the question that has the run stopped. */
export function useApprovalDecision(sessionId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: "once" | "always" | "deny" }) =>
      api.resolveApproval(sessionId, id, decision),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.approvals(sessionId) });
      // "always" widens the allowlist, and the commands screen shows it.
      void client.invalidateQueries({ queryKey: keys.commands });
    },
  });
}

export function useSessionLifecycle() {
  const client = useQueryClient();
  return {
    create: useMutation({
      mutationFn: (body: { name: string; project_dir?: string; goal?: string }) =>
        api.createSession(body),
      onSuccess: (session) => putSession(client, session),
    }),
    remove: useMutation({
      mutationFn: ({ sessionId, files }: { sessionId: string; files?: boolean }) =>
        api.deleteSession(sessionId, files),
      onSuccess: (_result, { sessionId }) => {
        client.removeQueries({ queryKey: keys.session(sessionId) });
        client.removeQueries({ queryKey: keys.events(sessionId) });
        void client.invalidateQueries({ queryKey: keys.sessions });
      },
    }),
  };
}

// ------------------------------------------------------------------ agents

export function useAgentMutations(sessionId: string) {
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
        api.saveAgent(sessionId, name, body),
      onSuccess: settle,
    }),
    remove: useMutation({
      mutationFn: ({ name, force = false }: { name: string; force?: boolean }) =>
        api.deleteAgent(sessionId, name, force),
      onSuccess: settle,
    }),
    reset: useMutation({ mutationFn: (name: string) => api.resetAgent(sessionId, name),
                         onSuccess: settle }),
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
    remove: useMutation({ mutationFn: ({ name, force = false }: { name: string; force?: boolean }) =>
        api.deletePreset(name, force), onSuccess: settle }),
    rename: useMutation({
      mutationFn: ({ name, to }: { name: string; to: string }) => api.renamePreset(name, to),
      onSuccess: settle,
    }),
    check: useMutation({ mutationFn: api.checkPreset }),
    discover: useMutation({ mutationFn: api.discoverModels }),
  };
}

// ------------------------------------------------------------------- loops

export function useLoopMutations(sessionId: string) {
  const client = useQueryClient();
  const settle = () => void client.invalidateQueries({ queryKey: keys.loops });
  return {
    save: useMutation({
      mutationFn: ({ name, body }: { name: string; body: Partial<Loop> }) =>
        api.saveLoop(sessionId, name, body),
      onSuccess: settle,
    }),
    remove: useMutation({ mutationFn: ({ name, force = false }: { name: string; force?: boolean }) =>
        api.deleteLoop(sessionId, name, force),
                          onSuccess: settle }),
  };
}

// ---------------------------------------------------------------- commands

export function useCommandMutations(sessionId: string) {
  const client = useQueryClient();
  const settle = () => void client.invalidateQueries({ queryKey: keys.commands });
  return {
    save: useMutation({
      mutationFn: ({ name, body }: { name: string;
                                     body: { allowed?: string[]; shell?: boolean } }) =>
        api.saveCommandList(sessionId, name, body),
      onSuccess: settle,
    }),
    remove: useMutation({
      mutationFn: (name: string) => api.deleteCommandList(sessionId, name),
      onSuccess: settle }),
    allow: useMutation({
      mutationFn: (body: { programs: string[]; agent?: string }) =>
        api.allowProgram(sessionId, body),
      onSuccess: settle }),
    reset: useMutation({ mutationFn: () => api.resetCommands(sessionId),
                         onSuccess: settle }),
    cancel: useMutation({ mutationFn: api.cancelCommand }),
  };
}

// -------------------------------------------------------------- settings

export function useSettingsMutations(sessionId: string) {
  const client = useQueryClient();
  return {
    planning: useMutation({
      mutationFn: (body: Partial<Planning>) => api.setPlanning(sessionId, body),
      onSuccess: () => client.invalidateQueries({ queryKey: keys.settings(sessionId) }),
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

export function useRevertStep(sessionId: string) {
  const client = useQueryClient();
  const settle = () => {
    void client.invalidateQueries({ queryKey: keys.files(sessionId) });
    void client.invalidateQueries({ queryKey: keys.session(sessionId) });
    void client.invalidateQueries({ queryKey: ["commitLog", sessionId] });
  };
  return {
    revert: useMutation({
      mutationFn: (stepId: string) => api.revertStep(sessionId, stepId),
      onSuccess: settle,
    }),
    apply: useMutation({
      mutationFn: (stepId: string) => api.applyStep(sessionId, stepId),
      onSuccess: settle,
    }),
  };
}

export function useFileMutations(sessionId: string) {
  const client = useQueryClient();
  return {
    clear: useMutation({
      mutationFn: () => api.clearFiles(sessionId),
      onSuccess: () => {
        void client.invalidateQueries({ queryKey: keys.files(sessionId) });
        void client.invalidateQueries({ queryKey: keys.session(sessionId) });
      },
    }),
    write: useMutation({
      mutationFn: (body: { path: string; content: string }) => api.writeFile(sessionId, body),
      onSuccess: (result, body) => {
        // Put what was saved into the cache, rather than invalidating and
        // hoping. The editor drops its draft the moment the save returns, so
        // between that and the refetch landing it would fall back to the old
        // cached content — which reads as the save having done nothing.
        client.setQueryData(keys.file(sessionId, body.path), {
          path: body.path,
          content: body.content,
          bytes: result?.bytes ?? new TextEncoder().encode(body.content).length,
          lines: body.content.split("\n").length,
        });
        void client.invalidateQueries({ queryKey: keys.files(sessionId) });
      },
    }),
    remove: useMutation({
      mutationFn: (path: string) => api.deleteFile(sessionId, path),
      onSuccess: (_result, path) => {
        client.removeQueries({ queryKey: keys.file(sessionId, path) });
        void client.invalidateQueries({ queryKey: keys.files(sessionId) });
      },
    }),
  };
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
    /** What starts this project, according to its own README. */
    plan: useMutation({ mutationFn: () => api.planPreview(sessionId) }),
    /** Run it, rather than serve its files. Deliberately separate: this starts
     *  a build on the machine trance runs on. */
    run: useMutation({
      mutationFn: (plan: { command: string; dir: string }) =>
        api.startPreview(sessionId, { mode: "dev", ...plan }),
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
