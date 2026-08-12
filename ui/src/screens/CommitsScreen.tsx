/** What each request became — the project's own timeline.
 *
 * A request becomes a plan, the plan becomes a run, and the run becomes
 * commits. The by-request mode shows one item per iteration: the user's own
 * words as the title, the screenshots its run produced as the face, and —
 * expanded — the reply, the steps, the commits and the files. Each item is
 * also a point in time the project can go back to: rewind moves the branch
 * there (the abandoned tip is kept on a branch), and serve runs that exact
 * version so a bug can be chased to the iteration that introduced it.
 */

import { useState } from "react";
import { api } from "@/api/client";
import { useCommitLog, useMessageCommits, usePreview, useRequestHistory,
  useSession } from "@/api/queries";
import { useIterationActions, usePreviewMutations, useStartRun }
  from "@/api/mutations";
import type { RequestItem } from "@/api/types";
import { duration, timeOf } from "@/lib/format";
import { useUi } from "@/store/ui";
import { cn } from "@/lib/cn";
import { CommitRow } from "@/components/Commits";
import { Confirm } from "@/components/ui/Confirm";
import { copyText } from "@/lib/clipboard";
import { toast } from "@/components/Toaster";
import { stepTone } from "@/components/Shell";
import { Badge, Button, Dot, Empty, Panel, PanelHeader, Spinner }
  from "@/components/ui/primitives";

export function CommitsScreen() {
  const { sessionId, commitsFor, go } = useUi();
  const session = useSession(sessionId);
  const [mode, setMode] = useState<"requests" | "log">("requests");
  const log = useCommitLog(sessionId, mode === "log");
  const history = useRequestHistory(sessionId, mode === "requests");
  // Which card is open. Arriving from a reply opens that one; arriving by tab
  // opens the newest, which is the one you would have clicked.
  const [picked, setPicked] = useState<string | null>(null);
  const showing = picked ?? commitsFor ?? history.data?.[0]?.reply_id ?? null;

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

  return (
    <div className="h-full overflow-y-auto p-3">
      <Panel className="mx-auto max-w-4xl">
        <PanelHeader
          title="What each request became"
          subtitle="one item per iteration, newest first — expand for the plan, the commits and the files"
          actions={
            <>
              {switcher}
              <Button size="sm" onClick={() => go("home")}>back to the chat</Button>
            </>
          }
        />
        <div className="space-y-2 p-3">
          {history.isLoading && <Spinner className="m-3 text-muted" />}
          {history.data?.length === 0 && (
            <Empty
              title="Nothing has been asked for yet."
              hint="Describe a feature or a bug on the chat page. When the orchestrator turns it into a plan, what the run commits for it shows up here."
            />
          )}
          {history.data?.map((item) => (
            <IterationCard
              key={item.reply_id}
              item={item}
              sessionId={sessionId!}
              running={session.data?.status === "running"}
              expanded={item.reply_id === showing}
              onToggle={() => setPicked(
                item.reply_id === showing ? "" : item.reply_id)}
            />
          ))}
        </div>
      </Panel>
    </div>
  );
}

function IterationCard(
  { item, sessionId, running, expanded, onToggle }:
  {
    item: RequestItem; sessionId: string; running: boolean;
    expanded: boolean; onToggle: () => void;
  },
) {
  return (
    <div className={cn("rounded-[--radius] border",
                       expanded ? "border-accent/40" : "border-line")}>
      <button
        onClick={onToggle}
        className="flex w-full items-center gap-3 px-3 py-2 text-left hover:bg-panel-2"
      >
        <span className="w-3 shrink-0 text-xs text-muted">{expanded ? "▾" : "▸"}</span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm">
            {item.request || "(no request text)"}
          </span>
          <span className="mt-0.5 block text-[11px] text-muted">
            {timeOf(item.ts)} · {item.commit_count} commit(s)
            · {item.file_count} file(s)
            {item.worked_seconds > 0 && ` · worked ${duration(item.worked_seconds)}`}
            {item.still_to_run > 0 && ` · ${item.still_to_run} step(s) still to run`}
          </span>
        </span>
        {item.shots.length > 0 && (
          <span className="flex shrink-0 gap-1">
            {item.shots.slice(0, 4).map((shot) => (
              <img
                key={shot}
                src={api.shotUrl(sessionId, shot)}
                alt=""
                className="h-10 w-14 rounded-[--radius-sm] border border-line object-cover"
              />
            ))}
          </span>
        )}
      </button>
      {expanded && (
        <IterationDetail item={item} sessionId={sessionId} running={running} />
      )}
    </div>
  );
}

function IterationDetail(
  { item, sessionId, running }:
  { item: RequestItem; sessionId: string; running: boolean },
) {
  const { go, openFile, setOpenStep, focusPlanStep } = useUi();
  const answer = useMessageCommits(sessionId, item.reply_id);
  const start = useStartRun(sessionId);
  const acts = useIterationActions(sessionId);
  // The one preview the session has — the same one the files page drives, so
  // stop and share here are the same stop and share there.
  const preview = usePreview(sessionId);
  const { stop, share } = usePreviewMutations(sessionId);
  const servingThis = preview.data?.of_message === item.reply_id
    && Boolean(preview.data?.url || preview.data?.port);
  const [confirming, setConfirming] = useState(false);
  const data = answer.data;
  const pending = (data?.steps ?? []).filter((step) => step.status === "pending");

  return (
    <div className="space-y-4 border-t border-line p-4">
      {answer.isLoading && <Spinner className="m-2 text-muted" />}

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

      <div className="flex flex-wrap items-center gap-1.5">
        {!servingThis && (
          <Button
            size="sm"
            title={"Run this exact version: a checkout of where this iteration "
              + "ended, served the way the files page serves — replacing whatever "
              + "is currently being served. For chasing a bug to the iteration "
              + "that introduced it."}
            busy={acts.serve.isPending}
            onClick={() => acts.serve.mutateAsync(item.reply_id)
              .then((out) => {
                toast.ok(`Serving the project as of ${out.version} at ${out.open}`);
                if (out.open) window.open(out.open, "_blank");
              })
              .catch((error) => toast.err(String(error)))}
          >▶ run this version</Button>
        )}
        {servingThis && (
          <>
            <a href={preview.data?.open || preview.data?.url} target="_blank"
               rel="noreferrer">
              <Badge tone="ok">serving {preview.data?.version}</Badge>
            </a>
            <Button
              size="sm" busy={share.isPending}
              title="A public link to this version, through a tunnel — same as the files page"
              onClick={() => share.mutateAsync(undefined)
                .then(async (result) => {
                  if (!result.url) return;
                  const copied = await copyText(result.url);
                  toast.ok(copied
                    ? `Share link copied: ${result.url}`
                    : `Share link (copy it by hand): ${result.url}`);
                })
                .catch((error) => toast.err(String(error)))}
            >share</Button>
            <Button
              size="sm" variant="danger" busy={stop.isPending}
              title="Stop serving this version (takes the tunnel with it)"
              onClick={() => stop.mutateAsync()
                .then(() => toast.ok("Stopped."))
                .catch((error) => toast.err(String(error)))}
            >stop</Button>
          </>
        )}
        <Button
          size="sm" variant="danger"
          title={"Put the project back to exactly where this iteration left "
            + "it. The abandoned work is kept on a branch."}
          disabled={running}
          onClick={() => setConfirming(true)}
        >⏪ rewind here</Button>
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

      <Confirm
        open={confirming}
        title="Rewind the project to this point?"
        confirmLabel="Rewind"
        danger
        busy={acts.rewind.isPending}
        onClose={() => setConfirming(false)}
        onConfirm={() => {
          setConfirming(false);
          acts.rewind.mutateAsync(item.reply_id)
            .then((out) => toast.ok(
              `Rewound to ${out.to.slice(0, 8)}. Everything after is on branch `
              + `${out.kept_branch}; the chat and plan continue from here.`))
            .catch((error) => toast.err(String(error)));
        }}
      >
        <p>
          The branch and the working tree move back to where this iteration
          ended, and the chat and plan below it are trimmed — the session
          continues from this point. Nothing is destroyed: the abandoned tip
          stays on a branch, and the trimmed chat is archived in the session
          directory.
        </p>
      </Confirm>

      {data && data.steps.length > 0 && (
        <section>
          <h3 className="mb-1 text-xs font-medium text-muted">The plan it produced</h3>
          <div className="space-y-1 rounded-[--radius] border border-line p-2">
            {data.steps.map((step) => (
              <div key={step.id} className="flex items-center gap-2 text-sm">
                <button
                  className="flex min-w-0 flex-1 items-center gap-2 rounded-[--radius-sm]
                             px-1 py-0.5 text-left transition-colors hover:bg-panel-2"
                  title="Open this step on the run page — its history, its console"
                  onClick={() => { setOpenStep(step.id); go("run"); }}
                >
                  <Dot tone={stepTone(step.status)}
                       pulse={step.status === "running"} />
                  <span className="min-w-0 flex-1 truncate">{step.task}</span>
                  <Badge>{step.status}</Badge>
                </button>
                <button
                  className="shrink-0 px-1 text-muted hover:text-fg"
                  title="Edit this step on the plan page"
                  onClick={() => focusPlanStep(step.id)}
                >✎</button>
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
            <div className="space-y-px rounded-[--radius] border border-line p-1">
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
            {data.files.length} file(s) changed in total — click one to open it
          </h3>
          <ul className="font-code space-y-0.5 text-xs">
            {data.files.map((path) => (
              <li key={path}>
                <button
                  className="text-accent hover:underline"
                  title={`Open ${path} on the files page`}
                  onClick={() => { openFile(path); go("files"); }}
                >{path}</button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
