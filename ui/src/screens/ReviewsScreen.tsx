/** Every review sent, and what came of it.
 *
 * The list is summaries — what was asked for, whether the step has run, and the
 * commits it produced. A patch is fetched only when a commit is opened, because
 * a review that touched thirty files is thirty patches nobody is going to read
 * at once.
 *
 * The newest review is open and the rest folded: the one you just sent is the
 * one you came to check.
 */

import { useState } from "react";
import { useCommit, useReviews } from "@/api/queries";
import { useUi } from "@/store/ui";
import { cn } from "@/lib/cn";
import { timeOf } from "@/lib/format";
import { Badge, Empty, Panel, PanelHeader, Spinner } from "@/components/ui/primitives";
import type { StepStatus } from "@/api/types";

interface ReviewRound {
  review: string;
  at: string;
  status: StepStatus | "gone";
  notes: { path?: string; line?: number | null; note: string }[];
  before: string;
  after: string;
  files: string[];
  commits: { sha: string; short: string; subject: string; when: string; who: string }[];
}

const STATUS_TONE: Record<string, "neutral" | "accent" | "ok" | "err" | "warn"> = {
  pending: "neutral", running: "accent", done: "ok",
  failed: "err", halted: "err", skipped: "warn", gone: "neutral",
};

export function ReviewsScreen() {
  const sessionId = useUi((state) => state.sessionId);
  const reviews = useReviews(sessionId);

  if (!sessionId) return <Empty title="No session selected." />;

  const rounds = (reviews.data ?? []) as unknown as ReviewRound[];

  return (
    <div className="h-full overflow-y-auto p-3">
      <div className="mx-auto max-w-4xl space-y-3">
        {reviews.isLoading && <Spinner className="m-6 text-muted" />}

        {!reviews.isLoading && !rounds.length && (
          <Panel>
            <Empty
              title="No reviews sent yet."
              hint="On the Files page, comment on a line or on the project as a whole, then press Finish. The notes go back to the team as a step, and what it changes shows up here."
            />
          </Panel>
        )}

        {rounds.map((round, index) => (
          <ReviewCard key={round.review} round={round} openByDefault={index === 0} />
        ))}
      </div>
    </div>
  );
}

function ReviewCard({ round, openByDefault }: { round: ReviewRound; openByDefault: boolean }) {
  const [open, setOpen] = useState(openByDefault);

  return (
    <Panel>
      <PanelHeader
        title={
          <button onClick={() => setOpen(!open)} className="flex items-center gap-2 text-left">
            <span className="text-xs text-muted">{open ? "▾" : "▸"}</span>
            <span>{round.notes.length} note{round.notes.length === 1 ? "" : "s"}</span>
          </button>
        }
        subtitle={[round.at && timeOf(round.at),
                   `${round.commits.length} commit(s)`,
                   round.files.length ? `${round.files.length} file(s) changed` : ""]
          .filter(Boolean).join(" · ")}
        actions={<Badge tone={STATUS_TONE[round.status] ?? "neutral"}>{round.status}</Badge>}
      />

      {open && (
        <div className="space-y-4 p-4">
          <section className="space-y-1.5">
            <h4 className="text-[11px] font-semibold uppercase tracking-wide text-muted">
              what was asked for
            </h4>
            {round.notes.map((note, index) => (
              <div key={index} className="rounded-[--radius] bg-panel-2 p-2 text-sm">
                {note.path
                  ? <span className="text-accent">
                      {note.path}{note.line ? `:${note.line}` : ""} —{" "}
                    </span>
                  : <span className="text-muted">the project as a whole — </span>}
                {note.note}
              </div>
            ))}
          </section>

          <section className="space-y-1.5">
            <h4 className="text-[11px] font-semibold uppercase tracking-wide text-muted">
              what changed
            </h4>
            {!round.commits.length && (
              <p className="text-xs text-muted">
                {round.status === "done" || round.status === "failed"
                  ? "Nothing was committed for this review."
                  : "Nothing yet — the step has not finished."}
              </p>
            )}
            {round.commits.map((commit) => (
              <CommitRow key={commit.sha} commit={commit} />
            ))}
          </section>
        </div>
      )}
    </Panel>
  );
}

function CommitRow(
  { commit }: { commit: { sha: string; short: string; subject: string; when: string;
                          who: string } },
) {
  const sessionId = useUi((state) => state.sessionId);
  const [open, setOpen] = useState(false);
  // Fetched only once opened, and kept forever after: a commit cannot change.
  const patch = useCommit(sessionId, open ? commit.sha : null);

  return (
    <div className={cn("rounded-[--radius] border",
                       open ? "border-line" : "border-transparent")}>
      <button
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left hover:bg-panel-2"
      >
        <span className="w-3 shrink-0 text-xs text-muted">{open ? "▾" : "▸"}</span>
        <code className="shrink-0 text-xs text-accent">{commit.short}</code>
        <span className="min-w-0 flex-1 truncate text-sm">{commit.subject}</span>
        <span className="shrink-0 text-[11px] text-muted">{commit.when}</span>
      </button>

      {open && (
        <div className="px-2 pb-2">
          {patch.isLoading && <Spinner className="m-2 text-muted" />}
          {patch.isError && (
            <p className="p-2 text-xs text-err">
              That commit could not be read ({String(patch.error)}).
            </p>
          )}
          {patch.data && (
            <>
              {patch.data.stat && (
                <pre className="font-code mb-1 text-muted">{patch.data.stat}</pre>
              )}
              <Diff diff={patch.data.diff} />
              {patch.data.clipped && (
                <p className="mt-1 text-xs text-muted">
                  The patch is longer than this shows — open it with git for the rest.
                </p>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

function Diff({ diff }: { diff: string }) {
  return (
    <pre className="font-code max-h-[28rem] overflow-auto rounded-[--radius] bg-bg/60 p-2">
      {(diff ?? "").split("\n").map((line, index) => (
        <div
          key={index}
          className={cn(
            line.startsWith("+") && !line.startsWith("+++") && "bg-ok-soft text-ok",
            line.startsWith("-") && !line.startsWith("---") && "bg-err-soft text-err",
            line.startsWith("@@") && "text-purple",
            line.startsWith("diff --git") && "mt-2 text-accent",
          )}
        >
          {line || " "}
        </div>
      ))}
    </pre>
  );
}
