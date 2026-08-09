/** The live run: what the agents are doing, and what any one step actually did.
 *
 * Two panels with one rule between them. The console is the *stream* — bounded,
 * live, and never fetched in full. The step panel is *history* — fetched for the
 * one step you opened, when you open it. The old UI blurred these and pushed
 * every event of the whole run down the socket, which is how one step of a long
 * session became a 13MB page load.
 */

import { useEffect, useMemo, useRef } from "react";
import { useEventTail, useSession, useStepEvents } from "@/api/queries";
import { useStepActions, useSteer } from "@/api/mutations";
import { useUi } from "@/store/ui";
import { cn } from "@/lib/cn";
import { clip } from "@/lib/format";
import { EventLine } from "@/components/EventLine";
import { Badge, Button, Dot, Empty, Input, Panel, PanelHeader, Spinner }
  from "@/components/ui/primitives";
import { toast } from "@/components/Toaster";
import type { Step, StepStatus } from "@/api/types";

const STEP_TONE: Record<StepStatus, "neutral" | "accent" | "ok" | "err" | "warn"> = {
  pending: "neutral", running: "accent", done: "ok",
  failed: "err", halted: "err", skipped: "warn",
};

export function RunScreen() {
  const { sessionId, openStep, setOpenStep } = useUi();
  const session = useSession(sessionId);
  const steps = session.data?.flow.steps ?? [];

  if (!sessionId) return <Empty title="No session selected." />;

  return (
    <div className="grid h-full min-w-0 grid-cols-[17rem_minmax(0,1fr)_21rem] gap-3 p-3">
      <StepRail steps={steps} selected={openStep} onSelect={setOpenStep} />
      <Console />
      <StepDetail stepId={openStep} />
    </div>
  );
}

function StepRail(
  { steps, selected, onSelect }:
  { steps: Step[]; selected: string | null; onSelect: (id: string | null) => void },
) {
  const hideFinished = useUi((s) => s.hideFinished);
  const toggle = useUi((s) => s.toggleHideFinished);
  const shown = hideFinished ? steps.filter((s) => s.status !== "done") : steps;

  return (
    <Panel className="flex min-h-0 min-w-0 flex-col">
      <PanelHeader
        title="Steps"
        subtitle={`${steps.filter((s) => s.status === "done").length} of ${steps.length} done`}
        actions={
          <Button variant="ghost" size="sm" onClick={toggle}
                  title="Hide steps that are already done">
            {hideFinished ? "show all" : "hide done"}
          </Button>
        }
      />
      <div className="min-h-0 flex-1 space-y-1 overflow-y-auto p-2">
        {shown.map((step, index) => (
          <button
            key={step.id}
            onClick={() => onSelect(step.id === selected ? null : step.id)}
            className={cn(
              "flex w-full items-start gap-2 rounded-[--radius] px-2 py-1.5 text-left",
              "transition-colors hover:bg-panel-2",
              step.id === selected && "bg-panel-2 ring-1 ring-accent/40",
            )}
          >
            <span className="mt-1"><Dot tone={STEP_TONE[step.status]}
                                        pulse={step.status === "running"} /></span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-xs text-muted">
                {index + 1}. {step.loop || step.role}
              </span>
              <span className="block truncate text-sm">{clip(step.task, 60)}</span>
            </span>
          </button>
        ))}
        {!shown.length && <Empty title="No steps yet." hint="Plan the work first." />}
      </div>
    </Panel>
  );
}

function Console() {
  const { sessionId, showReads, toggleReads, consoleStep } = useUi();
  const tail = useEventTail(sessionId);
  const bottom = useRef<HTMLDivElement>(null);

  const events = useMemo(() => {
    const all = tail.data ?? [];
    return consoleStep ? all.filter((event) => event.step_id === consoleStep) : all;
  }, [tail.data, consoleStep]);

  // Follow the tail, which is what you want while watching a run. Only when
  // already at the bottom: yanking the view while someone is reading an earlier
  // failure is worse than not following at all.
  useEffect(() => {
    const node = bottom.current;
    if (!node) return;
    const box = node.parentElement;
    if (!box) return;
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
    if (atBottom) node.scrollIntoView({ block: "end" });
  }, [events.length]);

  return (
    <Panel className="flex min-h-0 min-w-0 flex-col">
      <PanelHeader
        title="Console"
        subtitle={consoleStep ? "scoped to one step" : "everything the agents do"}
        actions={
          <>
            {tail.isFetching && <Spinner className="text-muted" />}
            <Button variant="ghost" size="sm" onClick={toggleReads}>
              {showReads ? "hide reads" : "show reads"}
            </Button>
          </>
        }
      />
      <div className="min-h-0 flex-1 space-y-px overflow-y-auto p-2">
        {events.map((event) => (
          <EventLine key={event.id} event={event} sessionId={sessionId!} />
        ))}
        {!events.length && (
          <Empty
            title="Nothing yet."
            hint="The console shows what happens from now on. Start the run, or open a step to see what it already did."
          />
        )}
        <div ref={bottom} />
      </div>
    </Panel>
  );
}

function StepDetail({ stepId }: { stepId: string | null }) {
  const sessionId = useUi((s) => s.sessionId);
  const setConsoleStep = useUi((s) => s.setConsoleStep);
  const session = useSession(sessionId);
  const step = session.data?.flow.steps.find((s) => s.id === stepId) ?? null;
  const events = useStepEvents(sessionId, stepId);
  const { rerun, skip } = useStepActions(sessionId ?? "");
  const steer = useSteer(sessionId ?? "");

  if (!step) {
    return (
      <Panel className="flex min-h-0 min-w-0 flex-col">
        <PanelHeader title="Step" />
        <Empty title="Pick a step." hint="Its history is fetched when you open it, not held for every step of the run." />
      </Panel>
    );
  }

  return (
    <Panel className="flex min-h-0 min-w-0 flex-col">
      <PanelHeader
        title={step.loop || step.role}
        subtitle={clip(step.task, 90)}
        actions={<Badge tone={STEP_TONE[step.status]}>{step.status}</Badge>}
      />

      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
        <div className="flex flex-wrap gap-1.5">
          <Button size="sm" onClick={() => setConsoleStep(step.id)}>focus console</Button>
          <Button size="sm" busy={rerun.isPending}
                  onClick={() => rerun.mutateAsync(step.id).catch((e) => toast.err(String(e)))}>
            rerun
          </Button>
          <Button size="sm" busy={skip.isPending}
                  onClick={() => skip.mutateAsync(step.id).catch((e) => toast.err(String(e)))}>
            skip
          </Button>
        </div>

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
          <Input name="note" placeholder="steer this step…" className="h-8 text-xs" />
          <Button size="sm" type="submit" busy={steer.isPending}>send</Button>
        </form>

        {step.attempts.map((attempt) => (
          <div key={attempt.n} className="space-y-1 rounded-[--radius] border border-line p-2">
            <div className="flex items-center gap-2 text-xs">
              <span className="text-muted">attempt {attempt.n}</span>
              {attempt.outcome && (
                <Badge tone={attempt.outcome === "SUCCESS" ? "ok" : "err"}>
                  {attempt.outcome}
                </Badge>
              )}
              {attempt.files_written.length > 0 && (
                <span className="truncate text-muted">
                  {attempt.files_written.join(", ")}
                </span>
              )}
            </div>
            {attempt.outcome_reason && (
              <p className="text-xs leading-snug text-err">{attempt.outcome_reason}</p>
            )}
          </div>
        ))}

        <div className="space-y-px">
          {events.isLoading && <Spinner className="text-muted" />}
          {events.data?.map((event) => (
            <EventLine key={event.id} event={event} sessionId={sessionId!} />
          ))}
          {events.data?.length === 0 && (
            <Empty
              title="Nothing was recorded for this step."
              hint="It ran before this session kept a trace on disk, so its history existed only in the page that was open at the time."
            />
          )}
        </div>
      </div>
    </Panel>
  );
}
