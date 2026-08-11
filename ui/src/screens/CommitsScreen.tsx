/** What one request turned into.
 *
 * A request becomes a plan, the plan becomes a run, and the run becomes
 * commits. Each of those was visible on its own screen and none of them was
 * connected to the one before, so "what actually came of what I asked for" was
 * a question you answered by remembering. The orchestrator's reply records
 * where the code stood when it was written; everything committed between there
 * and the next request is the answer, and this is where it is read.
 */

import { useState } from "react";
import { useCommitLog, useMessageCommits, useSession } from "@/api/queries";
import { duration } from "@/lib/format";
import { useUi } from "@/store/ui";
import { cn } from "@/lib/cn";
import { useStartRun } from "@/api/mutations";
import { CommitRow } from "@/components/Commits";
import { toast } from "@/components/Toaster";
import { stepTone } from "@/components/Shell";
import { Badge, Button, Dot, Empty, Panel, PanelHeader, Spinner }
  from "@/components/ui/primitives";

export function CommitsScreen() {
  const { sessionId, commitsFor, go } = useUi();
  const session = useSession(sessionId);
  const asked = session.data?.chat ?? [];

  // Reached from a reply, or from the tab. Arriving by tab means no reply was
  // chosen, so it opens on the most recent request that produced work — which
  // is the one you would have clicked.
  const latest = [...asked].reverse().find((message) => message.base)?.id ?? null;
  const showing = commitsFor ?? latest;
  const answer = useMessageCommits(sessionId, showing);
  // Above the early return: hooks cannot be conditional, and this page has one
  // — a session whose chat has not loaded renders the empty state first, then
  // re-renders with a request to show.
  const start = useStartRun(sessionId ?? "");
  // Two questions this page can answer, as modes: "what came of what I asked"
  // (requests coupled to their steps and commits) and "what is actually in
  // the repo" — plain git history, which includes the user's own commits and
  // the clears, which no request owns.
  const [mode, setMode] = useState<"requests" | "log">("requests");
  const log = useCommitLog(sessionId, mode === "log");

  const switcher = (
    <div className="flex rounded-[--radius] border border-line p-0.5 text-xs">
      {([["requests", "By request"], ["log", "All commits"]] as const)
        .map(([id, label]) => (
          <button
            key={id}
            onClick={() => setMode(id)}
            className={mode === id
              ? "rounded-[--radius] bg-accent-soft px-2 py-1 text-fg"
              : "rounded-[--radius] px-2 py-1 text-muted hover:text-fg"}
          >{label}</button>
        ))}
    </div>
  );

  if (mode === "log") {
    return (
      <div className="h-full overflow-y-auto p-3">
        <Panel className="mx-auto max-w-4xl">
          <PanelHeader
            title="Every commit"
            subtitle="the project's git history, newest first — open one for its diff"
            actions={switcher}
          />
          <div className="space-y-px p-2">
            {log.isLoading && <Spinner className="m-2 text-muted" />}
            {log.data?.length === 0 && (
              <Empty title="No commits yet."
                     hint="Each finished step commits; they will appear as the run works." />
            )}
            {log.data?.map((commit) => <CommitRow key={commit.sha} commit={commit} />)}
          </div>
        </Panel>
      </div>
    );
  }

  if (!showing) {
    return (
      <Empty
        title="Nothing has been asked for yet."
        hint="Describe a feature or a bug on the chat page. When the orchestrator turns it into a plan, what the run commits for it shows up here."
      />
    );
  }

  const data = answer.data;
  // Only the steps this request added — running is offered for its own work,
  // not for whatever else is pending elsewhere in the plan.
  const pending = (data?.steps ?? []).filter((step) => step.status === "pending");
  // What this iteration has cost so far, in working time — summed from its own
  // steps, so a request that reused old work is not billed for it.
  const worked = (data?.steps ?? [])
    .reduce((sum, step) => sum + (step.seconds ?? 0), 0);
  // The request is the message before the reply, and it is the thing you
  // actually recognise — the reply is the orchestrator agreeing with it.
  const replyAt = asked.findIndex((message) => message.id === showing);
  const request = replyAt > 0 ? asked[replyAt - 1] : null;

  return (
    <div className="h-full overflow-y-auto p-3">
      <Panel className="mx-auto max-w-4xl">
        <PanelHeader
          title="What came of this"
          subtitle={data
            ? `${data.commits.length} commit(s)`
              + (data.still_to_run ? ` · ${data.still_to_run} step(s) still to run` : "")
            : "reading the history"}
          actions={
            <>
              {switcher}
              <Button size="sm" onClick={() => go("home")}>back to the chat</Button>
            </>
          }
        />

        <div className="space-y-4 p-4">
          {request && (
            <div className="rounded-[--radius] bg-accent-soft px-3 py-2">
              <div className="mb-1 text-[11px] uppercase tracking-wide text-muted">
                you asked
              </div>
              <p className="whitespace-pre-wrap text-sm leading-relaxed">
                {request.content}
              </p>
            </div>
          )}

          {data && (
            <div className="rounded-[--radius] border border-line bg-panel-2 px-3 py-2">
              <div className="mb-1 text-[11px] uppercase tracking-wide text-muted">
                the orchestrator said
              </div>
              <p className="whitespace-pre-wrap text-sm leading-relaxed">
                {data.message.content}
              </p>
            </div>
          )}

          {answer.isLoading && <Spinner className="m-3 text-muted" />}
          {answer.isError && (
            <p className="text-sm text-err">
              That could not be read ({String(answer.error)}).
            </p>
          )}

          {data && data.steps.length > 0 && (
            <section>
              <div className="mb-1 flex items-baseline gap-2">
                <h3 className="text-xs font-medium text-muted">
                  The plan it produced
                  {worked > 0 && (
                    <span className="ml-2 font-normal"
                          title="Working time across these steps: every attempt, fix and check">
                      · worked on for {duration(worked)}
                    </span>
                  )}
                </h3>
                <div className="flex-1" />
                {pending.length > 0 && (
                  <Button
                    size="sm" variant="primary" busy={start.isPending}
                    title={`Start the run — ${pending.length} of these have not run yet`}
                    onClick={() => start.mutateAsync()
                      .then(() => go("run"))
                      .catch((error) => toast.err(String(error)))}
                  >Run {pending.length} pending</Button>
                )}
              </div>
              {/* The steps are the plan; clicking them goes to the plan, where
                  they can be edited. A list you can read but not act on is a
                  dead end at exactly the moment you want to change something. */}
              <button
                onClick={() => go("plan")}
                title="Open the plan"
                className="w-full space-y-1 rounded-[--radius] border border-line p-2
                           text-left transition-colors hover:bg-panel-2"
              >
                {data.steps.map((step) => (
                  <span key={step.id} className="flex items-center gap-2 text-sm">
                    <Dot tone={stepTone(step.status)}
                         pulse={step.status === "running"} />
                    <span className="min-w-0 flex-1 truncate">{step.task}</span>
                    <Badge>{step.status}</Badge>
                  </span>
                ))}
              </button>
            </section>
          )}

          {data && (
            <section>
              <h3 className="mb-1 text-xs font-medium text-muted">
                The commits, oldest first
              </h3>
              {data.commits.length === 0 ? (
                <Empty
                  title="Nothing committed yet."
                  hint={data.still_to_run
                    ? "The steps this asked for have not finished. The commits appear "
                      + "here as each agent finishes — one per step."
                    : data.base
                      ? "The run made no commits for this request."
                      : "This project is not a git repository, so there is nothing to "
                        + "show. Turn on git in settings and later requests will be "
                        + "recorded."}
                />
              ) : (
                <div className={cn("space-y-px rounded-[--radius] border border-line p-1")}>
                  {data.commits.map((commit) => (
                    <CommitRow key={commit.sha} commit={commit} />
                  ))}
                </div>
              )}
            </section>
          )}

          {data && data.files.length > 0 && (
            <section>
              <h3 className="mb-1 text-xs font-medium text-muted">
                {data.files.length} file(s) changed in total
              </h3>
              <ul className="font-code space-y-0.5 text-xs text-accent">
                {data.files.map((path) => <li key={path}>{path}</li>)}
              </ul>
            </section>
          )}
        </div>
      </Panel>
    </div>
  );
}
