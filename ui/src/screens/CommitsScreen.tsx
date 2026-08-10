/** What one request turned into.
 *
 * A request becomes a plan, the plan becomes a run, and the run becomes
 * commits. Each of those was visible on its own screen and none of them was
 * connected to the one before, so "what actually came of what I asked for" was
 * a question you answered by remembering. The orchestrator's reply records
 * where the code stood when it was written; everything committed between there
 * and the next request is the answer, and this is where it is read.
 */

import { useMessageCommits, useSession } from "@/api/queries";
import { useUi } from "@/store/ui";
import { cn } from "@/lib/cn";
import { CommitRow } from "@/components/Commits";
import { stepTone } from "@/components/Shell";
import { Badge, Button, Dot, Empty, Panel, PanelHeader, Spinner }
  from "@/components/ui/primitives";

export function CommitsScreen() {
  const { sessionId, commitsFor, go } = useUi();
  const session = useSession(sessionId);
  const answer = useMessageCommits(sessionId, commitsFor);

  if (!commitsFor) {
    return (
      <Empty
        title="Nothing chosen yet."
        hint="Open the chat and press “what came of this” on a reply that proposed work."
      />
    );
  }

  const data = answer.data;
  const asked = session.data?.chat ?? [];
  // The request is the message before the reply, and it is the thing you
  // actually recognise — the reply is the orchestrator agreeing with it.
  const replyAt = asked.findIndex((message) => message.id === commitsFor);
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
              <Button size="sm" onClick={() => go("home")}>back to the chat</Button>
              <Button size="sm" onClick={() => go("run")}>the run</Button>
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
              <h3 className="mb-1 text-xs font-medium text-muted">
                The steps it added
              </h3>
              <div className="space-y-1">
                {data.steps.map((step) => (
                  <div key={step.id} className="flex items-center gap-2 text-sm">
                    <Dot tone={stepTone(step.status)}
                         pulse={step.status === "running"} />
                    <span className="min-w-0 flex-1 truncate">{step.task}</span>
                    <Badge>{step.status}</Badge>
                  </div>
                ))}
              </div>
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
