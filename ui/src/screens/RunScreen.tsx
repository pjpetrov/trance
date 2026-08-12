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
import { useRevertStep, useRunControl, useStartRun, useStepActions, useSteer }
  from "@/api/mutations";
import { useUi } from "@/store/ui";
import { cn } from "@/lib/cn";
import { clip, timeOf } from "@/lib/format";
import { currentRun, isLookup, splitIntoBlocks, splitIntoRuns, tallyOf, type Block, type Run }
  from "@/lib/runs";
import { EventLine } from "@/components/EventLine";
import { ContextGauge } from "@/components/ContextGauge";
import { Badge, Button, Dot, Empty, Input, Panel, PanelHeader, Spinner }
  from "@/components/ui/primitives";
import { stepTone, stepWord } from "@/components/Shell";
import { toast } from "@/components/Toaster";
import { Confirm } from "@/components/ui/Confirm";
import type { Step, TranceEvent } from "@/api/types";

export function RunScreen() {
  const { sessionId, openStep, setOpenStep, setFollow } = useUi();
  const session = useSession(sessionId);
  const steps = session.data?.flow.steps ?? [];

  // Which run is being shown, when the user has picked one from the history.
  // Null means "the latest", which is what following a live step wants.
  const [pinnedRun, setPinnedRun] = useState<number | null>(null);

  // Opening a different step always shows that step's latest run.
  useEffect(() => { setPinnedRun(null); }, [openStep]);

  // Arriving on this page means "show me the work", and the work is a step:
  // whatever is running, else the last step that actually ran, else the last
  // step of the plan. With nothing selected the console fell back to a
  // session-wide feed — lines from whichever agent was mid-sentence, with no
  // way to tell which step they came from.
  //
  // Once per visit rather than once per session. Coming back to this page is
  // asking the same question again, and the answer has usually changed: the
  // step you opened it on ten minutes ago has finished and another is running.
  // Within a visit it never fights you — clicking a step keeps that step.
  const picked = useRef(false);
  useEffect(() => {
    if (!sessionId || picked.current) return;
    const target = steps.find((step) => step.status === "running")
      ?? [...steps].reverse().find((step) => step.runs > 0)
      ?? steps[steps.length - 1];
    if (!target) return;                       // no plan yet; nothing to open
    picked.current = true;
    setOpenStep(target.id);
  }, [sessionId, steps, setOpenStep]);

  if (!sessionId) return <Empty title="No session selected." />;

  return (
    <div className="grid h-full min-w-0 grid-cols-[20rem_minmax(0,1fr)]
                    grid-rows-[auto_minmax(0,1fr)] gap-3 p-3">
      <div className="col-span-2 -mb-1 flex items-center gap-2">
        <RunControls />
      </div>
      <StepRail
        steps={steps} selected={openStep}
        // Clicking the open step used to close it, which left the console
        // showing a session-wide feed nobody asked for. One step is always
        // open; picking a different one is the only thing a click does.
        //
        // And picking a different one pauses following, exactly as scrolling
        // up does: both say "I am reading this". Without it, following would
        // take the step away again at the next transition, for no visible
        // reason. The switch says so, and turning it back on resumes.
        onSelect={(id) => {
          if (id !== openStep) setFollow(false);
          setOpenStep(id);
        }}
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
  const commits = useRevertStep(sessionId ?? "");
  // Which of the two commit operations is awaiting the user's yes.
  const [confirming, setConfirming] = useState<"revert" | "apply" | null>(null);
  const steer = useSteer(sessionId ?? "");
  const [showRuns, setShowRuns] = useState(false);

  // Only the open step's history is fetched, and only when its run list is
  // asked for — a rail of twenty steps must not be twenty requests.
  const events = useStepEvents(sessionId, open ? step.id : null);
  const runs = useMemo(
    () => splitIntoRuns(Array.isArray(events.data) ? events.data : []), [events.data]);

  // A twelve-step plan is taller than the rail, and the step that is running is
  // the one at the bottom. Selecting it without showing it is the same as not
  // selecting it.
  const row = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (open) row.current?.scrollIntoView({ block: "nearest" });
  }, [open]);

  return (
    <div ref={row}
         className={cn("rounded-[--radius] transition-colors",
                       open ? "bg-panel-2 ring-1 ring-accent/40" : "hover:bg-panel-2")}>
      <button onClick={onSelect} className="flex w-full items-start gap-2 px-2 py-1.5 text-left">
        <span className="mt-1">
          <Dot tone={stepTone(step.status)} pulse={step.status === "running" || step.status === "verifying"} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-xs text-muted">
            {n}. {step.loop || step.role}
            {step.runs > 1 && ` · ${step.runs} runs`}
            {stepWord(step.status) && ` · ${stepWord(step.status)}`}
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
            {step.attempts.some((attempt) => attempt.commit) && (
              <Button
                size="sm" variant="danger" busy={commits.revert.isPending}
                title="Undo everything this step committed, as one inverse commit — the work and the undo both stay in history"
                onClick={() => setConfirming("revert")}
              >revert commits</Button>
            )}
            {step.reverted_sha && (
              <Button
                size="sm" busy={commits.apply.isPending}
                title="Take the revert back: re-apply this step's commits"
                onClick={() => setConfirming("apply")}
              >apply commits</Button>
            )}
          </div>

          <Confirm
            open={confirming === "revert"}
            title="Revert this step's commits?"
            confirmLabel="Revert them"
            danger
            busy={commits.revert.isPending}
            onClose={() => setConfirming(null)}
            onConfirm={() => {
              setConfirming(null);
              commits.revert.mutateAsync(step.id)
                .then((out) => toast.ok(
                  `Reverted ${out.reverted.length} commit(s) as ${out.sha.slice(0, 8)}. `
                  + "Apply commits takes it back."))
                // A failed revert changed nothing; the button stays for
                // another try once the conflict is dealt with.
                .catch((error) => toast.err(String(error)))}}
          >
            <p>
              Everything this step committed is undone as one inverse commit. The
              work and the undo both stay in history, and <b>apply commits</b> can
              take the revert back.
            </p>
          </Confirm>

          <Confirm
            open={confirming === "apply"}
            title="Re-apply this step's commits?"
            confirmLabel="Apply them"
            busy={commits.apply.isPending}
            onClose={() => setConfirming(null)}
            onConfirm={() => {
              setConfirming(null);
              commits.apply.mutateAsync(step.id)
                .then((out) => toast.ok(`Re-applied as ${out.sha.slice(0, 8)}.`))
                .catch((error) => toast.err(String(error)))}}
          >
            <p>
              The revert is taken back: this step's work returns, and all three
              acts — the work, the revert, the change of mind — stay in history.
            </p>
          </Confirm>

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
  const { sessionId, showReads, toggleReads, follow, setFollow, setOpenStep } = useUi();
  const session = useSession(sessionId);
  const steps = session.data?.flow.steps ?? [];
  const tail = useEventTail(sessionId);
  const stepEvents = useStepEvents(sessionId, stepId);
  const bottom = useRef<HTMLDivElement>(null);

  // The step query answers with a bare list and the tail with an envelope, and
  // splitting an envelope yields no runs at all — a console that is silently
  // empty rather than wrong. Ask before iterating.
  const runs = useMemo(
    () => splitIntoRuns(Array.isArray(stepEvents.data) ? stepEvents.data : []),
    [stepEvents.data]);

  const shown: Run | null = useMemo(() => {
    if (!stepId) return null;
    if (pinnedRun !== null) return runs.find((run) => run.n === pinnedRun) ?? null;
    return currentRun(runs);
  }, [stepId, pinnedRun, runs]);

  // With no step open, the console is the session's recent history — which is
  // what it always was, and what "show me the last execution" wants when you
  // arrive on the page rather than having clicked anything.
  const all = stepId ? (shown?.events ?? []) : (tail.data ?? []);
  // "show reads" changed its own label and nothing else, so a repair turn that
  // made 84 lookups and 3 edits buried the edits in a wall of lookups and the
  // button that was supposed to fix that did nothing.
  const hidden = useMemo(
    () => (showReads ? 0 : all.filter(isLookup).length), [all, showReads]);
  const events = useMemo(
    () => (showReads ? all : all.filter((event) => !isLookup(event))), [all, showReads]);
  const blocks = useMemo(() => splitIntoBlocks(events), [events]);
  const loading = stepId ? stepEvents.isLoading : tail.isLoading;

  // The newest event, but only while something is actually going: a finished
  // run whose last event happens to be a waiting one must not sit there
  // counting up the hours since it stopped.
  // Nothing is waiting for a model unless the session is actually running. The
  // run's own "still going" is about its events, and events cannot know the
  // engine stopped.
  const going = session.data?.status === "running"
    && (stepId ? Boolean(shown?.running) : true);
  const liveId = going && events.length ? events[events.length - 1]!.id : null;

  // The command that started and has not ended. Only its ending was ever
  // drawn, so a step blocked on one showed nothing for the three minutes it
  // took the timeout to kill it.
  const liveCommand = useMemo(() => {
    if (!going) return null;
    const over = new Set(events
      .filter((event) => event.type === "command_finished")
      .map((event) => event.payload?.command_id));
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const event = events[index]!;
      if (event.type === "command_started" && !over.has(event.payload?.command_id)) {
        return event.id;
      }
    }
    return null;
  }, [events, going]);

  // Two different scrolls. Arriving at a run puts you at its end, because the
  // last thing that happened is what you opened it to read. After that the
  // console only follows if you are already at the bottom — yanking the view
  // while someone reads an earlier failure is worse than not following.
  const landedOn = `${stepId ?? "session"}:${shown?.n ?? ""}:${loading}`;
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [landedOn]);

  // Following used to be inferred from where the scrollbar happened to be, so
  // it stopped for reasons nothing showed and nothing could turn back on. It
  // is a switch now: this only asks whether it is on.
  useEffect(() => {
    if (follow) bottom.current?.scrollIntoView({ block: "end" });
  }, [events.length, follow]);

  // Scrolling up is how anyone says "stop moving, I am reading this". Scrolling
  // back down does not turn it on again — the switch does, so that what it says
  // is always what is happening.
  const onScroll = (moved: React.UIEvent<HTMLDivElement>) => {
    const box = moved.currentTarget;
    if (follow && box.scrollHeight - box.scrollTop - box.clientHeight > 160) {
      setFollow(false);
    }
  };

  return (
    <Panel className="flex min-h-0 min-w-0 flex-col">
      <PanelHeader
        title="Console"
        subtitle={stepId
          ? (shown
            ? `${shown.label}${going ? " — still going" : ""}` +
              (shown.startedAt ? ` · started ${timeOf(shown.startedAt)}` : "")
            : "nothing recorded for this step")
          : "everything the agents have done"}
        actions={
          <>
            <ModelState events={events} going={going} />
            {(tail.isFetching || stepEvents.isFetching) && <Spinner className="text-muted" />}
            {stepId && pinnedRun !== null && (
              <Button size="sm" onClick={() => onPickRun(null)}>latest run</Button>
            )}
            <Button
              size="sm"
              variant={follow ? "primary" : "ghost"}
              title={follow
                ? "Following the run: newest lines stay in view, and the console "
                  + "moves to whichever step is working. Scrolling up pauses it."
                : "Paused. Nothing moves on its own until you turn this back on."}
              onClick={() => {
                const next = !follow;
                setFollow(next);
                // Turning it on means "show me what is happening now" — the
                // running step, its newest run, its newest lines. Only jumping
                // at the *next* transition left you reading the step you had
                // wandered to, following in name only until something changed.
                if (next) {
                  const live = steps.find((step) => step.status === "running"
                                                 || step.status === "verifying");
                  if (live && live.id !== stepId) setOpenStep(live.id);
                  onPickRun(null);
                  bottom.current?.scrollIntoView({ block: "end" });
                }
              }}
            >{follow ? "following" : "follow"}</Button>
            <Button variant="ghost" size="sm" onClick={toggleReads}>
              {showReads ? "hide reads" : `show reads${hidden ? ` (${hidden})` : ""}`}
            </Button>
          </>
        }
      />
      <div className="min-h-0 flex-1 space-y-px overflow-y-auto p-2" onScroll={onScroll}>
        {loading && <Spinner className="m-3 text-muted" />}

        {stepId
          ? blocks.map((block, index) => (
              <AgentBlock
                key={block.key} block={block} sessionId={sessionId!}
                stepId={stepId!} canRerun={session.data?.status !== "running"}
                // The one still going is what you are watching; the finished
                // ones fold to a line saying how they went. A run of eight
                // blocks is otherwise a wall you have to scroll past to reach
                // the part that is live.
                openByDefault={block.running || index === blocks.length - 1}
                liveId={liveId} liveCommand={liveCommand}
              />
            ))
          : events.map((event) => (
              <EventLine key={event.id} event={event} sessionId={sessionId!}
                         live={event.id === liveId || event.id === liveCommand} />
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

/** What the model is doing, and how full its window is.
 *
 *  A local 27B can spend two minutes on one generation and says nothing while
 *  it does, so the console goes quiet and there is no way to tell working from
 *  stuck. The runner now announces the call before making it; this turns that
 *  into a line that counts, and clears itself when the answer lands.
 */
function ModelState({ events, going }: { events: TranceEvent[]; going: boolean }) {
  const [now, setNow] = useState(() => Date.now());

  // The last thing that said anything about a model call, either way round.
  const last = useMemo(() => {
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const event = events[index]!;
      if (event.type === "model_waiting" || event.type === "model_call") return event;
    }
    return null;
  }, [events]);

  // Waiting only counts while something is running. A run that stopped on a
  // waiting event left the header spinning and counting up the hours since,
  // which says "working" about a session that has been dead all night.
  const waiting = going && last?.type === "model_waiting";
  // Both events carry the gauge: the waiting one sizes it from an estimate so
  // it moves while the model thinks, the finished one replaces it with what the
  // model actually reported. Read separately from the newest event that has one
  // rather than off `last`, because a single event without the reading — one
  // emitted before this existed, or by a call that forgot it — would otherwise
  // blank a gauge that was up a second ago. The last reading is still true.
  const context = useMemo(() => {
    for (let index = events.length - 1; index >= 0; index -= 1) {
      const found = events[index]!.payload?.context;
      if (found) return found;
    }
    return null;
  }, [events]);

  useEffect(() => {
    if (!waiting) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [waiting]);

  if (!last) return null;
  const since = waiting
    ? Math.max(0, Math.round((now - new Date(last.ts).getTime()) / 1000))
    : 0;

  return (
    <span className="flex items-center gap-3">
      {waiting && (
        <span className="flex items-center gap-1.5 text-[11px] text-accent">
          <Spinner className="size-2.5" />
          waiting for {String(last.payload?.preset || last.payload?.model || "the model")}
          {since > 2 && <span className="tabular-nums text-muted">{since}s</span>}
        </span>
      )}
      {context && <ContextGauge context={context} />}
    </span>
  );
}

/** One agent's turn, folded once it is over.
 *
 *  Folded it says who ran, how it went and one line of why — which is what you
 *  want from the seven blocks you are not currently reading. */
function AgentBlock(
  { block, sessionId, stepId, canRerun, openByDefault, liveId, liveCommand }:
  {
    block: Block; sessionId: string; stepId: string; canRerun: boolean;
    openByDefault: boolean; liveId: string | null; liveCommand: string | null;
  },
) {
  const [open, setOpen] = useState(openByDefault);
  const { rerunBlock } = useStepActions(sessionId);
  const [confirming, setConfirming] = useState(false);
  // What it spent itself on. On the header rather than inside, because the
  // question it answers — "what is it doing so much?" — is asked about a block
  // you have not opened.
  const tally = useMemo(() => tallyOf(block.events), [block.events]);
  // A block that was folded stays folded when it finishes; one you are watching
  // opens itself when it starts.
  useEffect(() => { if (openByDefault) setOpen(true); }, [openByDefault]);

  const good = /SUCCESS|PASS/.test(block.outcome);
  // Which attempt this block was, engine-numbered — the handle "rerun from
  // here" needs. Only loop blocks carry it, and only ones recorded since the
  // engine started stamping it.
  const marker = block.events[0];
  const attempt = marker?.type === "loop_node"
    ? (marker.payload as { attempt?: number })?.attempt : undefined;

  return (
    <div className={cn("rounded-[--radius] border",
                       open ? "border-line" : "border-transparent",
                       block.running && "border-accent/40")}>
      <div className="flex w-full items-center gap-2 hover:bg-panel-2">
        <button
          onClick={() => setOpen(!open)}
          className="flex min-w-0 flex-1 items-center gap-2 px-2 py-1.5 text-left"
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
          {tally && (
            <span className="shrink-0 text-[11px] text-muted/80">{tally}</span>
          )}
          <span className="ml-auto shrink-0 text-[11px] text-muted">
            {timeOf(block.startedAt)}
          </span>
        </button>
        {attempt !== undefined && canRerun && !block.running && (
          <Button
            size="sm" variant="ghost" className="mr-1 shrink-0"
            title={`Rewind to this block and run ${block.agent} again with the same handoff — `
              + "change its model in Agents first if that is the point"}
            onClick={() => setConfirming(true)}
          >↻ rerun</Button>
        )}
      </div>

      <Confirm
        open={confirming}
        title={`Rerun ${block.agent} from this block?`}
        confirmLabel="Rewind and rerun"
        danger
        busy={rerunBlock.isPending}
        onClose={() => setConfirming(false)}
        onConfirm={() => {
          setConfirming(false);
          rerunBlock.mutateAsync({ stepId, attempt: attempt! })
            .then(() => toast.ok(
              `Rewound to ${block.agent}'s block — the loop continues from there.`))
            .catch((error) => toast.err(String(error)));
        }}
      >
        <p>
          Commits from this block on are undone as one inverse commit,{" "}
          <b>{block.agent}</b> runs again with the same handoff it had, and the
          loop routes on from its outcome — so the new result is still judged.
          To try a different model, change {block.agent} in Agents before
          confirming.
        </p>
      </Confirm>

      {open && (
        <div className="space-y-px px-1 pb-1">
          {block.events.map((event) => (
            <EventLine key={event.id} event={event} sessionId={sessionId}
                       live={event.id === liveId || event.id === liveCommand} />
          ))}
        </div>
      )}
    </div>
  );
}
