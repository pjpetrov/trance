/** Loops: a small state machine of agents, with exits.
 *
 * A node names a role and what it is there to do; each exit (SUCCESS, FAILED,
 * CHECK_FAILED) lists routes tried in order, each with its own visit budget.
 * The routes really are a list — the API returns an array per exit, and reading
 * it as a single edge silently drops every fallback after the first.
 */

import { useEffect, useState } from "react";
import { useAgents, useLoops } from "@/api/queries";
import { useLoopMutations } from "@/api/mutations";
import { cn } from "@/lib/cn";
import { Badge, Button, Empty, Field, Input, Select, Textarea }
  from "@/components/ui/primitives";
import { toast } from "@/components/Toaster";
import type { Loop, LoopNode } from "@/api/types";

/** What an exit means, so the editor can say it rather than showing a raw key. */
const EXITS: Record<string, string> = {
  SUCCESS: "the agent reported success",
  FAILED: "the agent reported failure",
  CHECK_FAILED: "a verifier rejected the work",
};

const STOPS = new Set(["exit", "fail"]);

export function LoopsEditor() {
  const loops = useLoops();
  const agents = useAgents();
  const { save, remove } = useLoopMutations();
  const [selected, setSelected] = useState("");
  const [draft, setDraft] = useState<Loop | null>(null);

  useEffect(() => {
    const list = loops.data ?? [];
    if (!list.length) return;
    const found = list.find((loop) => loop.name === selected) ?? list[0]!;
    if (found.name !== selected) setSelected(found.name);
    setDraft(structuredClone(found));
  }, [loops.data, selected]);

  if (!loops.data?.length) return <Empty title="No loops defined." />;

  const edit = (patch: Partial<Loop>) =>
    setDraft((current) => (current ? { ...current, ...patch } : current));

  const editNode = (id: string, patch: Partial<LoopNode>) =>
    setDraft((current) => current && {
      ...current,
      nodes: current.nodes.map((node) => (node.id === id ? { ...node, ...patch } : node)),
    });

  const targets = (loop: Loop) => [
    ...loop.nodes.map((node) => ({ value: node.id, label: node.role || node.id })),
    { value: "exit", label: "leave the loop — the step succeeded" },
    { value: "fail", label: "leave the loop — the step failed" },
  ];

  return (
    <div className="grid h-[70vh] grid-cols-[14rem_1fr]">
      <aside className="min-h-0 space-y-0.5 overflow-y-auto border-r border-line p-2">
        {loops.data.map((loop) => (
          <button
            key={loop.name}
            onClick={() => setSelected(loop.name)}
            className={cn(
              "block w-full truncate rounded-[--radius] px-2 py-1.5 text-left text-sm",
              "transition-colors hover:bg-panel-2",
              loop.name === selected && "bg-panel-2 ring-1 ring-accent/40",
            )}
          >
            {loop.name}
          </button>
        ))}
      </aside>

      {draft && (
        <div className="min-h-0 space-y-4 overflow-y-auto p-4">
          <Field label="Description" hint="The orchestrator reads this when choosing a loop.">
            <Input value={draft.description}
                   onChange={(event) => edit({ description: event.target.value })} />
          </Field>

          <Field label="What finishes this block"
                 hint="Given to every agent in the loop, so none of them thinks declaring success is what ends it.">
            <Textarea rows={2} value={draft.prompt}
                      onChange={(event) => edit({ prompt: event.target.value })} />
          </Field>

          <Field label="Most blocks"
                 hint="A hard ceiling on how many times agents run before the step gives up.">
            <Input type="number" min={1} className="w-24" value={draft.max_steps}
                   onChange={(event) => edit({ max_steps: Number(event.target.value) || 1 })} />
          </Field>

          <div className="space-y-2">
            <div className="text-xs font-medium text-muted">Blocks</div>
            {draft.nodes.map((node) => (
              <div key={node.id}
                   className="space-y-2 rounded-[--radius] border border-line bg-panel-2 p-3">
                <div className="flex items-center gap-2">
                  <Select
                    className="h-8 w-40"
                    value={node.role}
                    onChange={(event) => editNode(node.id, { role: event.target.value })}
                  >
                    {Object.values(agents.data?.byName ?? {})
                      .filter((role) => role.name !== "orchestrator")
                      .map((role) => (
                        <option key={role.name} value={role.name}>{role.name}</option>
                      ))}
                  </Select>
                  {draft.start === node.id
                    ? <Badge tone="accent">starts here</Badge>
                    : (
                      <Button size="sm" variant="ghost"
                              onClick={() => edit({ start: node.id })}>
                        start here
                      </Button>
                    )}
                </div>

                <Textarea
                  rows={2} className="text-xs"
                  placeholder="what this agent is here to do"
                  value={node.focus}
                  onChange={(event) => editNode(node.id, { focus: event.target.value })}
                />

                <div className="space-y-1">
                  {Object.entries(node.on).map(([exit, routes]) => (
                    <div key={exit} className="flex flex-wrap items-center gap-2 text-xs">
                      <span className="w-32 shrink-0 text-muted" title={EXITS[exit]}>
                        {exit.toLowerCase().replace("_", " ")}
                      </span>
                      {routes.map((route, index) => (
                        <span key={index} className="flex items-center gap-1">
                          <span className="text-muted">→</span>
                          <Select
                            className="h-7 w-56 text-xs"
                            value={route.target}
                            onChange={(event) => editNode(node.id, {
                              on: {
                                ...node.on,
                                [exit]: routes.map((held, at) => at === index
                                  ? { ...held, target: event.target.value } : held),
                              },
                            })}
                          >
                            {targets(draft).map((option) => (
                              <option key={option.value} value={option.value}>
                                {option.label}
                              </option>
                            ))}
                          </Select>
                          {!STOPS.has(route.target) && (
                            <span className="text-muted">
                              at most {route.max_visits ?? 3}×
                            </span>
                          )}
                        </span>
                      ))}
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>

          <div className="flex items-center gap-2 border-t border-line pt-3">
            <Button
              variant="primary" busy={save.isPending}
              onClick={() => save.mutateAsync({ name: draft.name, body: draft })
                .then(() => toast.ok(`Saved ${draft.name}.`))
                .catch((error) => toast.err(String(error)))}
            >Save</Button>
            <Button
              variant="danger" busy={remove.isPending}
              onClick={() => { if (confirm(`Delete the loop "${draft.name}"?`)) {
                remove.mutateAsync(draft.name)
                  .then(() => toast.ok("Deleted."))
                  // A loop a step still names cannot go; the server says so.
                  .catch((error) => toast.err(String(error)));
              } }}
            >Delete</Button>
            <div className="flex-1" />
            <span className="text-xs text-muted">
              {draft.nodes.length} block(s) · starts at{" "}
              {draft.nodes.find((node) => node.id === draft.start)?.role ?? "?"}
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
