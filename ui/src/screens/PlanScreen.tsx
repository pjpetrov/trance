/** The work order: which agent does what, and in what sequence.
 *
 * Editing saves as you go — there is no Save button, because a plan you edited
 * and forgot to save is a plan that runs wrong.
 */

import { useEffect, useState } from "react";
import { useAgents, useLoops, useSession } from "@/api/queries";
import { useSaveFlow, useStepActions } from "@/api/mutations";
import { useUi } from "@/store/ui";
import { cn } from "@/lib/cn";
import { Badge, Button, Empty, Input, Panel, PanelHeader, Select, Textarea }
  from "@/components/ui/primitives";
import { toast } from "@/components/Toaster";
import type { Step } from "@/api/types";

export function PlanScreen() {
  const sessionId = useUi((s) => s.sessionId);
  const session = useSession(sessionId);
  const agents = useAgents();
  const loops = useLoops();
  const save = useSaveFlow(sessionId ?? "");
  const { split } = useStepActions(sessionId ?? "");

  const [steps, setSteps] = useState<Step[]>([]);
  const [dragging, setDragging] = useState<number | null>(null);
  const [dropAt, setDropAt] = useState<number | null>(null);

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

  if (!sessionId) return <Empty title="No session selected." />;

  return (
    <div className="h-full overflow-y-auto p-3">
      <Panel className="mx-auto max-w-4xl">
        <PanelHeader
          title="Plan"
          subtitle={save.isPending ? "saving…" : "changes save as you make them"}
          actions={
            <Button
              variant="danger" size="sm"
              onClick={() => { if (confirm("Clear every step?")) persist([]); }}
            >Clear</Button>
          }
        />

        <div className="space-y-1 p-3">
          {steps.map((step, index) => (
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
                      {Object.values(agents.data?.byName ?? {})
                        .filter((role) => role.name !== "orchestrator")
                        .map((role) => (
                          <option key={role.name} value={`role:${role.name}`}>{role.name}</option>
                        ))}
                    </optgroup>
                    <optgroup label="loop">
                      {loops.data?.map((loop) => (
                        <option key={loop.name} value={`loop:${loop.name}`}>{loop.name}</option>
                      ))}
                    </optgroup>
                  </Select>

                  <Badge tone={step.status === "done" ? "ok"
                    : step.status === "failed" ? "err"
                    : step.status === "running" ? "accent" : "neutral"}>
                    {step.status}
                  </Badge>

                  <div className="flex-1" />

                  <Button
                    size="sm" variant="ghost"
                    onClick={() => split.mutateAsync(step.id)
                      .catch((error) => toast.err(String(error)))}
                  >split</Button>
                  <Button
                    size="sm" variant="ghost"
                    onClick={() => persist(steps.filter((_, at) => at !== index))}
                  >✕</Button>
                </div>

                <Textarea
                  rows={2}
                  className="mt-2 text-sm"
                  value={step.task}
                  onChange={(event) => {
                    const next = [...steps];
                    next[index] = { ...step, task: event.target.value };
                    setSteps(next);
                  }}
                  onBlur={() => persist(steps)}
                />
              </div>
            </div>
          ))}
          {dropAt === steps.length && <DropMarker />}

          {!steps.length && (
            <Empty
              title="No steps yet."
              hint="Describe the project on the Brief screen and the orchestrator will propose a plan."
            />
          )}

          <NewStep onAdd={(task) => persist([...steps, {
            ...blankStep(), id: `s_${Date.now().toString(36)}`, task,
            role: Object.keys(agents.data?.byName ?? { backend: null })[0] ?? "backend",
          }])} />
        </div>
      </Panel>
    </div>
  );
}

function DropMarker() {
  return <div className="my-0.5 h-0.5 rounded-full bg-accent" />;
}

function NewStep({ onAdd }: { onAdd: (task: string) => void }) {
  const [task, setTask] = useState("");
  return (
    <form
      className="flex gap-2 pt-2"
      onSubmit={(event) => {
        event.preventDefault();
        if (!task.trim()) return;
        onAdd(task.trim());
        setTask("");
      }}
    >
      <Input value={task} placeholder="add a step…"
             onChange={(event) => setTask(event.target.value)} />
      <Button type="submit">Add</Button>
    </form>
  );
}

function blankStep(): Step {
  return {
    id: "", role: "", task: "", loop: "", check: "", checks: [], checker: "", fixer: "",
    on_fail: null, verify_with: null, max_loops: 2, loop_limit: 0, max_attempts: 2,
    overrides_tries: false, start_on_backup: false, revert_on_fail: false, escalated: false,
    points: 0, gates: [], entry: "", status: "pending", attempts: [], runs: 0,
    steering: [],
    summary: "", runs_a_loop: false,
  };
}
