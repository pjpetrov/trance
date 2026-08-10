/** The work order: which agent does what, and in what sequence.
 *
 * Editing saves as you go — there is no Save button, because a plan you edited
 * and forgot to save is a plan that runs wrong.
 *
 * Steps that have already run are folded away rather than deleted. They are the
 * record of what happened and they are worth reusing: a step that worked once
 * is the easiest way to write the next one.
 */

import { useEffect, useState } from "react";
import { useAgents, useLoops, useSession } from "@/api/queries";
import { useChat, useSaveFlow, useStartRun, useStepActions } from "@/api/mutations";
import { useUi } from "@/store/ui";
import { cn } from "@/lib/cn";
import { clip } from "@/lib/format";
import { Badge, Button, Empty, Panel, PanelHeader, Select, Textarea }
  from "@/components/ui/primitives";
import { Modal } from "@/components/ui/Modal";
import { toast } from "@/components/Toaster";
import type { Step, StepStatus } from "@/api/types";

const STATUS_TONE: Record<StepStatus, "neutral" | "accent" | "ok" | "err" | "warn"> = {
  pending: "neutral", running: "accent", done: "ok",
  failed: "err", halted: "err", skipped: "warn",
};

/** What the orchestrator is asked when you press Generate. Kept here rather
 *  than typed each time, because "propose the plan" and "give me a plan now"
 *  get different amounts of thought out of a model. */
const GENERATE =
  "Propose the plan now, based on everything we have discussed. Call propose_flow.";

export function PlanScreen() {
  const { sessionId, go } = useUi();
  const session = useSession(sessionId);
  const agents = useAgents(sessionId ?? "");
  const loops = useLoops(sessionId ?? "");
  const save = useSaveFlow(sessionId ?? "");
  const start = useStartRun(sessionId ?? "");
  const chat = useChat(sessionId ?? "");
  const { split } = useStepActions(sessionId ?? "");

  const [steps, setSteps] = useState<Step[]>([]);
  const [dragging, setDragging] = useState<number | null>(null);
  const [dropAt, setDropAt] = useState<number | null>(null);
  const [showDone, setShowDone] = useState(false);
  const [generating, setGenerating] = useState(false);

  // The server is the source of truth; local edits are a draft on top of it.
  useEffect(() => {
    if (session.data) setSteps(session.data.flow.steps);
  }, [session.data]);

  const persist = (next: Step[]) => {
    setSteps(next);
    save.mutateAsync(next.map((step) => ({
      id: step.id, role: step.role, task: step.task, loop: step.loop,
      check: step.check, max_loops: step.max_loops,
    }))).catch((error) => toast.err(`Could not save the plan — ${error}`));
  };

  const move = (from: number, to: number) => {
    const next = [...steps];
    const [held] = next.splice(from, 1);
    if (held) next.splice(to > from ? to - 1 : to, 0, held);
    persist(next);
  };

  const firstAgent = agents.data?.agents.find((role) => role.name !== "orchestrator")?.name
    ?? "backend";

  const addStep = () => {
    const next = [...steps, { ...blankStep(), id: `s_${Date.now().toString(36)}`,
                              role: firstAgent }];
    persist(next);
  };

  const runIt = () => {
    // Just go there. The run page picks the step itself — whatever is running,
    // else the last one that ran — and picking it here as well meant two places
    // decided, with the first pending step winning over the one that started.
    start.mutateAsync()
      .then(() => go("run"))
      .catch((error) => toast.err(String(error)));
  };

  const generate = (fresh: boolean) => {
    setGenerating(false);
    const ask = fresh ? persist([]) : Promise.resolve();
    Promise.resolve(ask)
      .then(() => chat.mutateAsync({ message: GENERATE }))
      .then(() => toast.ok("Asked the orchestrator for a plan."))
      .catch((error) => toast.err(String(error)));
  };

  if (!sessionId) return <Empty title="No session selected." />;

  const done = steps.filter((step) => step.status !== "pending");
  const shown = showDone ? steps : steps.filter((step) => step.status === "pending");

  return (
    <div className="h-full overflow-y-auto p-3">
      <Panel className="mx-auto max-w-4xl">
        <PanelHeader
          title="Plan"
          subtitle={save.isPending ? "saving…" : "changes save as you make them"}
          actions={
            <>
              <Button size="sm" busy={chat.isPending}
                      onClick={() => setGenerating(true)}>Generate</Button>
              <Button size="sm" onClick={addStep}>Add a step</Button>
              <Button
                size="sm" variant="primary" busy={start.isPending}
                disabled={!steps.some((step) => step.status === "pending")}
                title={steps.some((step) => step.status === "pending")
                  ? "Run from the first pending step" : "Nothing is pending"}
                onClick={runIt}
              >Run</Button>
            </>
          }
        />

        {done.length > 0 && (
          <button
            onClick={() => setShowDone(!showDone)}
            className="flex w-full items-center gap-2 border-b border-line px-3 py-2
                       text-left text-xs text-muted hover:bg-panel-2"
          >
            <span className="w-3">{showDone ? "▾" : "▸"}</span>
            <span>
              {done.length} step{done.length === 1 ? "" : "s"} already ran —{" "}
              {showDone ? "hide them" : "show them to edit or reuse"}
            </span>
            <span className="ml-auto flex gap-1">
              {["done", "failed", "skipped", "halted"].map((status) => {
                const count = done.filter((step) => step.status === status).length;
                return count
                  ? <Badge key={status} tone={STATUS_TONE[status as StepStatus]}>
                      {count} {status}
                    </Badge>
                  : null;
              })}
            </span>
          </button>
        )}

        <div className="space-y-1 p-3">
          {shown.map((step) => {
            const index = steps.indexOf(step);
            return (
              <div key={step.id}>
                {dropAt === index && <DropMarker />}
                <div
                  draggable
                  onDragStart={() => setDragging(index)}
                  onDragEnd={() => { setDragging(null); setDropAt(null); }}
                  onDragOver={(event) => { event.preventDefault(); setDropAt(index); }}
                  onDrop={(event) => {
                    event.preventDefault();
                    if (dragging !== null) move(dragging, index);
                    setDragging(null); setDropAt(null);
                  }}
                  className={cn(
                    "rounded-[--radius] border border-line bg-panel-2 p-2.5",
                    "transition-opacity", dragging === index && "opacity-40",
                    step.status !== "pending" && "opacity-75",
                  )}
                >
                  <div className="flex items-center gap-2">
                    <span className="cursor-grab select-none px-1 text-muted"
                          title="Drag to reorder">⠿</span>
                    <span className="w-6 text-xs text-muted">{index + 1}</span>

                    <Select
                      className="h-8 w-44"
                      value={step.loop ? `loop:${step.loop}` : `role:${step.role}`}
                      onChange={(event) => {
                        const [kind, name] = event.target.value.split(":");
                        const next = [...steps];
                        next[index] = kind === "loop"
                          ? { ...step, loop: name!, role: "" }
                          : { ...step, loop: "", role: name! };
                        persist(next);
                      }}
                    >
                      <optgroup label="agent">
                        {(agents.data?.agents ?? [])
                          .filter((role) => role.name !== "orchestrator")
                          .map((role) => (
                            <option key={role.name} value={`role:${role.name}`}>
                              {role.name}
                            </option>
                          ))}
                      </optgroup>
                      <optgroup label="loop">
                        {loops.data?.map((loop) => (
                          <option key={loop.name} value={`loop:${loop.name}`}>{loop.name}</option>
                        ))}
                      </optgroup>
                    </Select>

                    <Badge tone={STATUS_TONE[step.status]}>{step.status}</Badge>

                    <div className="flex-1" />

                    <Button size="sm" variant="ghost"
                            onClick={() => split.mutateAsync(step.id)
                              .catch((error) => toast.err(String(error)))}>
                      split
                    </Button>
                    <Button
                      size="sm" variant="danger"
                      title="Remove this step from the plan"
                      onClick={() => {
                        if (step.status !== "pending"
                            && !confirm(`Delete step ${index + 1}? It has already run, and its `
                                        + "history goes with it.")) return;
                        persist(steps.filter((_, at) => at !== index));
                      }}
                    >Delete</Button>
                  </div>

                  <Textarea
                    rows={5}
                    className="mt-2 text-sm"
                    placeholder="what this step should do — name the files it will create or change"
                    value={step.task}
                    autoFocus={!step.task}
                    onChange={(event) => {
                      const next = [...steps];
                      next[index] = { ...step, task: event.target.value };
                      setSteps(next);
                    }}
                    onBlur={() => persist(steps)}
                  />
                </div>
              </div>
            );
          })}
          {dropAt === steps.length && <DropMarker />}

          {!shown.length && (
            <Empty
              title={done.length ? "Nothing pending." : "No steps yet."}
              hint={done.length
                ? "Every step has run. Add one, or open the finished ones above to reuse."
                : "Describe the project on the Chat page and press Generate, or add a step yourself."}
              action={<Button className="mt-2" onClick={addStep}>Add a step</Button>}
            />
          )}
        </div>
      </Panel>

      <Modal
        open={generating}
        onClose={() => setGenerating(false)}
        title="Ask the orchestrator for a plan"
        subtitle="It reads the whole conversation from the Chat page and proposes the work."
        footer={
          <>
            <Button onClick={() => setGenerating(false)}>Cancel</Button>
            <Button onClick={() => generate(true)} variant="danger">
              Discard and start fresh
            </Button>
            <Button variant="primary" onClick={() => generate(false)}>
              Keep what is here
            </Button>
          </>
        }
      >
        <div className="space-y-2 p-5 text-sm leading-relaxed">
          <p>
            <b>Keep what is here</b> adds to the plan. Steps that already ran are never
            touched, and anything the orchestrator proposes that matches work already done
            is skipped — which is what you want when asking for a new feature or a fix.
          </p>
          <p className="text-muted">
            <b>Discard and start fresh</b> clears every step first, including the ones that
            already ran. Their history stays in the run log, but they leave the plan.
          </p>
          {steps.length > 0 && (
            <p className="text-xs text-muted">
              {steps.length} step(s) on the plan now, {done.length} of which have run.
            </p>
          )}
        </div>
      </Modal>
    </div>
  );
}

function DropMarker() {
  return <div className="my-0.5 h-0.5 rounded-full bg-accent" />;
}

function blankStep(): Step {
  return {
    id: "", role: "", task: "", loop: "", check: "", checks: [], checker: "", fixer: "",
    on_fail: null, verify_with: null, max_loops: 2, loop_limit: 0, max_attempts: 2,
    overrides_tries: false, start_on_backup: false, revert_on_fail: false, escalated: false,
    points: 0, gates: [], entry: "", status: "pending", attempts: [], runs: 0,
    steering: [], summary: "", runs_a_loop: false,
  };
}

export { clip };
