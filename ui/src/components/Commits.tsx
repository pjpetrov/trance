/** A commit, and what it changed.
 *
 * Shared because the same question is asked from two directions: a review asks
 * "what did the agents do to the code I commented on", and a chat reply asks
 * "what came of what I requested". Same commits, same diffs, one renderer.
 */

import { useState } from "react";
import { useCommit } from "@/api/queries";
import { useUi } from "@/store/ui";
import { cn } from "@/lib/cn";
import { Spinner } from "@/components/ui/primitives";

export interface Commit {
  sha: string;
  short: string;
  subject: string;
  when: string;
  who: string;
}

export function CommitRow({ commit }: { commit: Commit }) {
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

export function Diff({ diff }: { diff: string }) {
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
