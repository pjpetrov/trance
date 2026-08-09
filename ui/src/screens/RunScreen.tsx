/** The live run: what the agents are doing, and what any one step did.
 *
 * Two panels, not three. The old third column repeated what the console already
 * showed, so it is gone and the actions it held sit next to the step they act
 * on. What the console shows is now a *run* — one press of Start or Rerun, with
 * every retry and every loop block inside it — because that is the unit anybody
 * means by "what happened last time".
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useEventTail, useSession, useStepEvents } from "@/api/queries";
import { useRunControl, useStartRun, useStepActions, useSteer } from "@/api/mutations";
import { useUi } from "@/store/ui";
import { cn } from "@/lib/cn";
import { clip, timeOf } from "@/lib/format";
import { currentRun, splitIntoBlocks, splitIntoRuns, type Block, type Run }
  from "@/lib/runs";
import { EventLine } from "@/components/EventLine";
import { Badge, Button, Dot, Empty, Input, Panel, PanelHeader, Spinner, type Tone }
  from "@/components/ui/primitives";
import { toast } from "@/components/Toaster";
import type { Step, StepStatus } from "@/api/types";

const STEP_TONE: Record<StepStatus, Tone> = {
  pending: "neutral", running: "accent", done: "ok",
  failed: "err", halted: "err", skipped: "warn",
};

export function RunScreen() {
  const { sessionId, openStep, setOpenStep } = useUi();
  const session = useSession(sessionId);
  const steps = session.data?.flow.steps ?? [];

  // Which run is being shown, when the user has picked one from the history.
  // Null means "the latest", which is what following a live step wants.
  const [pinnedRun, setPinnedRun] = useState<number | null>(null);

  // Opening a different step always shows that step's latest run.
  useEffect(() => { setPinnedRun(null); }, [openStep]);

  if (!sessionId) return <Empty title="No session selected." />;

  return (
    <div className="grid h-full min-w-0 grid-cols-[20rem_minmax(0,1fr)]
                    grid-rows-[auto_minmax(0,1fr)] gap-3 p-3">
      <div className="col-span-2 -mb-1 flex items-center gap-2">
        <RunControls />
      </div>
      <StepRail
        steps={steps} selected={openStep}
        onSelect={(id) => setOpenStep(id === openStep ? null : id)}
        pinnedRun={pinnedRun} onPickRun={setPinnedRun}
      />
      <Console stepId={openStep} pinnedRun={pinnedRun} onPickRun={setPinnedRun} />
    </div>
  );
}

/** Start, pause and stop, on the page that runs things.
 *
 *  They used to sit in the top bar beside Settings and Agents, which put "run
 *  the project" next to "edit an allowlist" and made it unclear which session
 *  they applied to. */
function RunControls() {
  const sessionId = useUi((state) => state.sessionId);
  const openModal = useUi((state) => state.openModal);
  const session = useSession(sessionId);
  const start = useStartRun(sessionId ?? "");
  const { pause, resume, stop } = useRunControl(sessionId ?? "");
  const live = session.data;
  if (!live) return null;

  const running = live.status === "running";
  const paused = Boolean(live.paused);
  const act = (action: { mutateAsync: () => Promise<unknown> }, what: string) => () =>
    action.mutateAsync().catch((error: unknown) => toast.err(`${what} failed — ${error}`));

  return (
    <>
      {!running && (
        <Button variant="primary" busy={start.isPending} onClick={act(start, "Start")}>
          Start the run
        </Button>
      )}
      {running && !paused && (
        <Button busy={pause.isPending} onClick={act(pause, "Pause")}>Pause</Button>
      )}
      {running && paused && (
        <Button variant="primary" busy={resume.isPending} onClick={act(resume, "Resume")}>
          Resume
        </Button>
      )}
      {running && (
        <Button variant="danger" busy={stop.isPending} onClick={act(stop, "Stop")}>Stop</Button>
      )}
      <span className="text-xs text-muted">
        {running ? (paused ? "paused" : "running") : "not running"}
        {" · "}{live.progress.done}/{live.progress.total} steps done
      </span>
      <div className="flex-1" />
      {/* What the agents are reading right now, so it belongs with the run
          rather than in a toolbar next to Settings. */}
      <Button size="sm" variant="ghost" onClick={() => openModal("memory")}>
        Project memory
      </Button>
    </>
  );
}

function StepRail(
  { steps, selected, onSelect, pinnedRun, onPickRun }:
  {
    steps: Step[]; selected: string | null; onSelect: (id: string) => void;
    pinnedRun: number | null; onPickRun: (run: number | null) => void;
  },
) {
  const hideFinished = useUi((state) => state.hideFinished);
  const toggle = useUi((state) => state.toggleHideFinished);
  // Finished means finished, however it went. Hiding only the successes left a
  // rail full of failures you had already read.
  const over = (step: Step) => step.status !== "pending" && step.status !== "running";
  const shown = hideFinished ? steps.filter((step) => !over(step)) : steps;
  const done = steps.filter((step) => step.status === "done").length;

  return (
    <Panel className="flex min-h-0 min-w-0 flex-col">
      <PanelHeader
        title="Steps"
        subtitle={`${done} of ${steps.length} done`}
        actions={
          <Button variant="ghost" size="sm" onClick={toggle}
                  title="Hide every step that has finished, however it went">
            {hideFinished ? "show all" : "hide finished"}
          </Button>
        }
      />
      <div className="min-h-0 flex-1 space-y-1 overflow-y-auto p-2">
        {shown.map((step) => (
          <StepRow
            key={step.id} step={step} n={steps.indexOf(step) + 1}
            open={step.id === selected}
            onSelect={() => onSelect(step.id)}
            pinnedRun={pinnedRun}
            onPickRun={onPickRun}
          />
        ))}
        {!shown.length && (
          <Empty title="No steps yet." hint="Plan the work first." />
        )}
      </div>
    </Panel>
  );
}

function StepRow(
  { step, n, open, onSelect, pinnedRun, onPickRun }:
  {
    step: Step; n: number; open: boolean; onSelect: () => void;
    pinnedRun: number | null; onPickRun: (run: number | null) => void;
  },
) {
  const sessionId = useUi((state) => state.sessionId);
  const { rerun } = useStepActions(sessionId ?? "");
  const steer = useSteer(sessionId ?? "");
  const [showRuns, setShowRuns] = useState(false);

  // Only the open step's history is fetched, and only when its run list is
  // asked for — a rail of twenty steps must not be twenty requests.
  const events = useStepEvents(sessionId, open ? step.id : null);
  const runs = useMemo(() => splitIntoRuns(events.data ?? []), [events.data]);

  return (
    <div className={cn("rounded-[--radius] transition-colors",
                       open ? "bg-panel-2 ring-1 ring-accent/40" : "hover:bg-panel-2")}>
      <button onClick={onSelect} className="flex w-full items-start gap-2 px-2 py-1.5 text-left">
        <span className="mt-1">
          <Dot tone={STEP_TONE[step.status]} pulse={step.status === "running"} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-xs text-muted">
            {n}. {step.loop || step.role}
            {step.runs > 1 && ` · ${step.runs} runs`}
          </span>
          <span className="block truncate text-sm">{clip(step.task, 64)}</span>
        </span>
      </button>

      {open && (
        <div className="space-y-2 px-2 pb-2">
          <div className="flex flex-wrap items-center gap-1.5">
            <Button
              size="sm" variant="primary" busy={rerun.isPending}
              title="Run this step now"
              onClick={() => rerun.mutateAsync(step.id)
                .then(() => { onPickRun(null); toast.ok(`Started ${step.loop || step.role}.`); })
                .catch((error) => toast.err(String(error)))}
            >start</Button>
            <Button size="sm" variant={showRuns ? "default" : "ghost"}
                    onClick={() => setShowRuns(!showRuns)}
                    title="Every run of this step">
              history{runs.length ? ` (${runs.length})` : ""}
            </Button>
          </div>

          {showRuns && (
            <div className="space-y-0.5 rounded-[--radius] border border-line p-1">
              {!runs.length && !events.isLoading && (
                <p className="px-1.5 py-1 text-xs text-muted">
                  Nothing recorded for this step yet.
                </p>
              )}
              {events.isLoading && <Spinner className="m-2 text-muted" />}
              {runs.slice().reverse().map((run) => (
                <button
                  key={run.n}
                  onClick={() => onPickRun(run.n)}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-[--radius-sm] px-1.5 py-1",
                    "text-left text-xs hover:bg-panel",
                    (pinnedRun ?? runs[runs.length - 1]?.n) === run.n && "text-accent",
                  )}
                >
                  <span className="flex-1 truncate">{run.label}</span>
                  <span className="text-muted">{timeOf(run.startedAt)}</span>
                  {run.running
                    ? <Badge tone="accent">running</Badge>
                    : run.outcome && (
                      <Badge tone={/SUCCESS|PASS/.test(run.outcome) ? "ok" : "err"}>
                        {run.outcome}
                      </Badge>
                    )}
                </button>
              ))}
            </div>
          )}

          <form
            className="flex gap-1.5"
            onSubmit={(event) => {
              event.preventDefault();
              const field = event.currentTarget.elements.namedItem("note") as HTMLInputElement;
              if (!field.value.trim()) return;
              steer.mutateAsync({ note: field.value, step_id: step.id })
                .then(() => { field.value = ""; toast.ok("Hint delivered."); })
                .catch((error) => toast.err(String(error)));
            }}
          >
            <Input name="note" placeholder="steer this step…" className="h-7 text-xs" />
            <Button size="sm" type="submit" busy={steer.isPending}>send</Button>
          </form>
        </div>
      )}
    </div>
  );
}

function Console(
  { stepId, pinnedRun, onPickRun }:
  { stepId: string | null; pinnedRun: number | null; onPickRun: (run: number | null) => void },
) {
  const { sessionId, showReads, toggleReads } = useUi();
  const tail = useEventTail(sessionId);
  const stepEvents = useStepEvents(sessionId, stepId);
  const bottom = useRef<HTMLDivElement>(null);

  const runs = useMemo(
    () => splitIntoRuns(stepEvents.data ?? []), [stepEvents.data]);

  const shown: Run | null = useMemo(() => {
    if (!stepId) return null;
    if (pinnedRun !== null) return runs.find((run) => run.n === pinnedRun) ?? null;
    return currentRun(runs);
  }, [stepId, pinnedRun, runs]);

  // With no step open, the console is the session's recent history — which is
  // what it always was, and what "show me the last execution" wants when you
  // arrive on the page rather than having clicked anything.
  const events = stepId ? (shown?.events ?? []) : (tail.data ?? []);
  const blocks = useMemo(() => splitIntoBlocks(events), [events]);
  const loading = stepId ? stepEvents.isLoading : tail.isLoading;

  // Two different scrolls. Arriving at a run puts you at its end, because the
  // last thing that happened is what you opened it to read. After that the
  // console only follows if you are already at the bottom — yanking the view
  // while someone reads an earlier failure is worse than not following.
  const landedOn = `${stepId ?? "session"}:${shown?.n ?? ""}:${loading}`;
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [landedOn]);

  useEffect(() => {
    const node = bottom.current;
    const box = node?.parentElement;
    if (!node || !box) return;
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
    if (atBottom) node.scrollIntoView({ block: "end" });
  }, [events.length]);

  return (
    <Panel className="flex min-h-0 min-w-0 flex-col">
      <PanelHeader
        title="Console"
        subtitle={stepId
          ? (shown
            ? `${shown.label}${shown.running ? " — still going" : ""}` +
              (shown.startedAt ? ` · started ${timeOf(shown.startedAt)}` : "")
            : "nothing recorded for this step")
          : "everything the agents have done"}
        actions={
          <>
            {(tail.isFetching || stepEvents.isFetching) && <Spinner className="text-muted" />}
            {stepId && pinnedRun !== null && (
              <Button size="sm" onClick={() => onPickRun(null)}>latest run</Button>
            )}
            <Button variant="ghost" size="sm" onClick={toggleReads}>
              {showReads ? "hide reads" : "show reads"}
            </Button>
          </>
        }
      />
      <div className="min-h-0 flex-1 space-y-px overflow-y-auto p-2">
        {loading && <Spinner className="m-3 text-muted" />}

        {stepId
          ? blocks.map((block, index) => (
              <AgentBlock
                key={block.key} block={block} sessionId={sessionId!}
                // The one still going is what you are watching; the finished
                // ones fold to a line saying how they went. A run of eight
                // blocks is otherwise a wall you have to scroll past to reach
                // the part that is live.
                openByDefault={block.running || index === blocks.length - 1}
              />
            ))
          : events.map((event) => (
              <EventLine key={event.id} event={event} sessionId={sessionId!} />
            ))}

        {!loading && !events.length && (
          <Empty
            title={stepId ? "Nothing was recorded for this step." : "Nothing yet."}
            hint={stepId
              ? "It ran before this session kept a trace on disk, so its history existed only in the page that was open at the time."
              : "Start the run, or open a step to see what it already did."}
          />
        )}
        <div ref={bottom} />
      </div>
    </Panel>
  );
}

/** One agent's turn, folded once it is over.
 *
 *  Folded it says who ran, how it went and one line of why — which is what you
 *  want from the seven blocks you are not currently reading. */
function AgentBlock(
  { block, sessionId, openByDefault }:
  { block: Block; sessionId: string; openByDefault: boolean },
) {
  const [open, setOpen] = useState(openByDefault);
  // A block that was folded stays folded when it finishes; one you are watching
  // opens itself when it starts.
  useEffect(() => { if (openByDefault) setOpen(true); }, [openByDefault]);

  const good = /SUCCESS|PASS/.test(block.outcome);

  return (
    <div className={cn("rounded-[--radius] border",
                       open ? "border-line" : "border-transparent",
                       block.running && "border-accent/40")}>
      <button
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left hover:bg-panel-2"
      >
        <span className="w-3 shrink-0 text-xs text-muted">{open ? "▾" : "▸"}</span>
        <span className="shrink-0 text-xs font-medium">{block.label}</span>
        {block.running
          ? <Badge tone="accent">running</Badge>
          : block.outcome
            ? <Badge tone={good ? "ok" : "err"}>{block.outcome}</Badge>
            : <Badge>done</Badge>}
        {!open && block.summary && (
          <span className="min-w-0 flex-1 truncate text-xs text-muted">{block.summary}</span>
        )}
        <span className="ml-auto shrink-0 text-[11px] text-muted">
          {timeOf(block.startedAt)}
        </span>
      </button>

      {open && (
        <div className="space-y-px px-1 pb-1">
          {block.events.map((event) => (
            <EventLine key={event.id} event={event} sessionId={sessionId} />
          ))}
        </div>
      )}
    </div>
  );
}
