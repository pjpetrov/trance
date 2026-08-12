/** Loops: a small state machine of agents, with exits.
 *
 * A node names a role and what it is there to do; each exit (SUCCESS, FAILED,
 * CHECK_FAILED) lists routes tried in order, each with its own visit budget.
 * The routes really are a list — the API returns an array per exit, and reading
 * it as a single edge silently drops every fallback after the first.
 */

import { useState } from "react";
import { useAgents, useLoops } from "@/api/queries";
import { useLoopMutations } from "@/api/mutations";
import { useDraftLibrary } from "@/lib/useDraftLibrary";
import { useUi } from "@/store/ui";
import { Badge, Button, Empty, Field, Input, Select, Textarea }
  from "@/components/ui/primitives";
import { ForceConfirm, LibraryFooter, LibraryList, useForcedRemoval }
  from "@/components/ui/Library";
import { DEFAULTS, ScopeSwitch, idFor, type Scope }
  from "@/components/ScopeSwitch";
import { toast } from "@/components/Toaster";
import { Checks } from "@/components/Checks";
import type { Loop, LoopEdge, LoopNode } from "@/api/types";

/** What an exit means, so the editor can say it rather than showing a raw key. */
const EXITS: Record<string, string> = {
  SUCCESS: "the agent reported success",
  FAILED: "the agent reported failure",
  CHECK_FAILED: "a verifier rejected the work",
};

const STOPS = new Set(["exit", "fail"]);

function blankLoop(taken: Set<string>): Loop {
  let name = "new-loop";
  for (let n = 2; taken.has(name); n += 1) name = `new-loop-${n}`;
  const first = `n_${Math.random().toString(36).slice(2, 8)}`;
  const second = `n_${Math.random().toString(36).slice(2, 8)}`;
  // Seeded with the shape people build by hand every time: someone checks, and
  // on a failure someone else fixes and it is checked again. An empty loop is a
  // blank state nobody knows what to do with.
  return {
    name,
    description: "",
    prompt: "This block is finished when the work is right — the verdict decides, "
      + "not a declaration of success.",
    start: first,
    max_steps: 8,
    nodes: [
      { id: first, role: "tester", focus: "Check the work and report what actually happened.",
        check: null, revert_on_fail: false,
        on: { SUCCESS: [{ target: "exit", max_visits: 3 }],
              FAILED: [{ target: second, max_visits: 3 }] } },
      { id: second, role: "developer",
        focus: "Fix what the check objected to. It runs again straight after you.",
        check: null, revert_on_fail: false,
        on: { SUCCESS: [{ target: first, max_visits: 3 }],
              FAILED: [{ target: "fail", max_visits: 3 }] } },
    ],
  };
}

export function LoopsEditor() {
  const session = useUi((state) => state.sessionId);
  const [scope, setScope] = useState<Scope>("project");
  const sessionId = idFor(scope, session);
  const loops = useLoops(sessionId);
  const agents = useAgents(sessionId);
  const { save, remove } = useLoopMutations(sessionId);
  const template = useLoopMutations(DEFAULTS);
  const [applying, setApplying] = useState(false);

  const library = useDraftLibrary<Loop>(loops.data, (loop) => loop.name);
  const draft = library.selected;

  const removals = useForcedRemoval(
    (name, force) => remove.mutateAsync({ name, force }));

  const apply = async (alsoDefault = false) => {
    setApplying(true);
    try {
      if (!(await removals.removeAll(library.removed))) return;
      for (const loop of library.changed) {
        await save.mutateAsync({ name: loop.name, body: loop });
        // Deletions are not repeated against the template — see AgentsEditor.
        if (alsoDefault) await template.save.mutateAsync({ name: loop.name, body: loop });
      }
      library.settle();
      toast.ok(`Applied ${library.changeCount} change(s)`
               + (alsoDefault ? ", here and for new sessions." : "."));
    } catch (error) {
      // A loop a step still names cannot be deleted; the server says so.
      toast.err(`${error}. Nothing else was applied.`);
    } finally {
      setApplying(false);
    }
  };

  if (loops.isLoading) return <Empty title="Loading loops…" />;

  const editNode = (id: string, patch: Partial<LoopNode>) => {
    if (!draft) return;
    library.replace({
      ...draft,
      nodes: draft.nodes.map((node) => (node.id === id ? { ...node, ...patch } : node)),
    });
  };

  const addBlock = () => {
    if (!draft) return;
    const id = `n_${Math.random().toString(36).slice(2, 8)}`;
    library.replace({
      ...draft,
      nodes: [...draft.nodes, {
        id, role: agents.data?.agents[0]?.name ?? "developer", focus: "", check: null,
        revert_on_fail: false,
        on: { SUCCESS: [{ target: "exit", max_visits: 3 }],
              FAILED: [{ target: "fail", max_visits: 3 }] },
      }],
    });
  };

  const targets = (loop: Loop) => [
    ...loop.nodes.map((node) => ({ value: node.id, label: node.role || node.id })),
    { value: "exit", label: "leave the loop — the step succeeded" },
    { value: "fail", label: "leave the loop — the step failed" },
  ];

  const isNew = draft ? library.isNew(draft.name) : false;

  return (
    <div className="flex h-[70vh] flex-col">
      <div className="grid min-h-0 flex-1 grid-cols-[14rem_minmax(0,1fr)]">
        <LibraryList
          items={library.items}
          nameOf={(loop) => loop.name}
          selected={library.selectedName}
          onSelect={library.select}
          isNew={library.isNew}
          removed={library.removed}
          addLabel="New loop"
          onAdd={() => library.add(
            blankLoop(new Set(library.items.map((loop) => loop.name))))}
          meta={(loop) => (
            <span className="shrink-0 text-[10px] text-muted">{loop.nodes.length}</span>
          )}
        />

        {draft && (
          <div className="min-h-0 space-y-4 overflow-y-auto p-4">
            <Field
              label="Name"
              hint={isNew ? "Steps reference loops by name; it cannot change later."
                          : "Fixed — steps reference this loop by name."}
            >
              <Input value={draft.name} disabled={!isNew}
                     onChange={(e) => library.replace({ ...draft, name: e.target.value })} />
            </Field>

            <Field label="Description" hint="The orchestrator reads this when choosing a loop.">
              <Input value={draft.description}
                     onChange={(e) => library.edit({ description: e.target.value })} />
            </Field>

            <Field
              label="What finishes this block"
              hint="Given to every agent in the loop, so none of them thinks declaring success is what ends it."
            >
              <Textarea rows={2} value={draft.prompt}
                        onChange={(e) => library.edit({ prompt: e.target.value })} />
            </Field>

            <Field label="Most blocks"
                   hint="A hard ceiling on how many times agents run before the step gives up.">
              <Input type="number" min={1} className="w-24" value={draft.max_steps}
                     onChange={(e) => library.edit({ max_steps: Number(e.target.value) || 1 })} />
            </Field>

            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <span className="text-xs font-medium text-muted">Blocks</span>
                <Button size="sm" variant="ghost" onClick={addBlock}>add a block</Button>
              </div>

              {draft.nodes.map((node) => (
                <div key={node.id}
                     className="space-y-2 rounded-[--radius] border border-line bg-panel-2 p-3">
                  <div className="flex items-center gap-2">
                    <Select
                      className="h-8 w-40" value={node.role}
                      onChange={(event) => editNode(node.id, { role: event.target.value })}
                    >
                      {(agents.data?.agents ?? [])
                        .filter((role) => role.name !== "orchestrator")
                        .map((role) => (
                          <option key={role.name} value={role.name}>{role.name}</option>
                        ))}
                    </Select>
                    {draft.start === node.id
                      ? <Badge tone="accent">starts here</Badge>
                      : (
                        <Button size="sm" variant="ghost"
                                onClick={() => library.edit({ start: node.id })}>
                          start here
                        </Button>
                      )}
                    <div className="flex-1" />
                    {draft.nodes.length > 1 && (
                      <Button
                        size="sm" variant="ghost"
                        onClick={() => library.replace({
                          ...draft,
                          nodes: draft.nodes.filter((held) => held.id !== node.id),
                        })}
                      >✕</Button>
                    )}
                  </div>

                  <Textarea
                    rows={2} className="text-xs"
                    placeholder="what this agent is here to do"
                    value={node.focus}
                    onChange={(event) => editNode(node.id, { focus: event.target.value })}
                  />

                  {/* The same chips a plan step carries, seeded from this
                      node's agent — and the loop's own to change: taking one
                      off here stays off. */}
                  <Checks
                    checks={node.checks ?? (node.check ? [node.check] : [])}
                    verifiers={(agents.data?.agents ?? [])
                      .filter((role) => role.verifier && role.name !== node.role)}
                    onChange={(checks) => editNode(node.id, {
                      checks, checks_seeded: true,
                      check: checks[0] ?? null,
                    })}
                  />

                  <div className="space-y-1">
                    {/* Every outcome, always — not only the ones already
                        routed. The old render was read-only in disguise:
                        routes could be re-aimed but never added, removed or
                        tiered, and the visit cap was a label. */}
                    {Object.keys(EXITS).map((exit) => {
                      const routes = node.on[exit] ?? [];
                      const write = (next: LoopEdge[]) => editNode(node.id, {
                        on: { ...node.on, [exit]: next },
                      });
                      return (
                        <div key={exit} className="flex flex-wrap items-center gap-2 text-xs">
                          <span className="w-32 shrink-0 text-muted" title={EXITS[exit]}>
                            {exit.toLowerCase().replace("_", " ")}
                          </span>
                          {routes.length === 0 && (
                            <span className="text-muted/70"
                                  title="An outcome with no route fails the loop — the safe default for a case nobody thought about.">
                              halts the loop
                            </span>
                          )}
                          {routes.map((route, index) => (
                            <span key={index} className="flex items-center gap-1">
                              <span className="text-muted">
                                {index === 0 ? "→" : "then →"}
                              </span>
                              <Select
                                className="h-7 w-48 text-xs" value={route.target}
                                onChange={(event) => write(routes.map((held, at) =>
                                  at === index
                                    ? { ...held, target: event.target.value } : held))}
                              >
                                {targets(draft).map((option) => (
                                  <option key={option.value} value={option.value}>
                                    {option.label}
                                  </option>
                                ))}
                              </Select>
                              {!STOPS.has(route.target) && (
                                <label className="flex items-center gap-1 text-muted"
                                       title="How many times this arrow may be taken before the next tier (or the halt).">
                                  at most
                                  <input
                                    type="number" min={1}
                                    className="h-7 w-12 rounded-[--radius] border border-line
                                               bg-bg px-1 text-center"
                                    value={route.max_visits ?? 3}
                                    onChange={(event) => write(routes.map((held, at) =>
                                      at === index
                                        ? { ...held, max_visits: Number(event.target.value) || 1 }
                                        : held))}
                                  />×
                                </label>
                              )}
                              {!STOPS.has(route.target) && (
                                <label className="flex items-center gap-1 text-muted"
                                       title="Take this tier on the agent's backup model — 'try again with something stronger' as its own arrow.">
                                  <input
                                    type="checkbox" checked={Boolean(route.backup)}
                                    onChange={(event) => write(routes.map((held, at) =>
                                      at === index
                                        ? { ...held, backup: event.target.checked }
                                        : held))}
                                  />backup
                                </label>
                              )}
                              <button
                                className="text-muted hover:text-err"
                                title="Remove this route"
                                onClick={() => write(routes.filter((_, at) => at !== index))}
                              >✕</button>
                            </span>
                          ))}
                          <Button
                            size="sm" variant="ghost"
                            title={routes.length
                              ? "Add a tier: when the arrows above are spent, this one is taken next"
                              : "Route this outcome"}
                            onClick={() => write([...routes,
                                                  { target: "exit", max_visits: 3 }])}
                          >+</Button>
                        </div>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>

            <Button size="sm" variant="danger" onClick={() => library.remove(draft.name)}>
              Delete this loop
            </Button>
          </div>
        )}
      </div>

      <footer className="flex items-center gap-2 border-t border-line px-4 py-3">
        <ScopeSwitch scope={scope} onChange={setScope} what="loops" />
        <div className="flex-1" />
        <ForceConfirm
          forcing={removals.forcing}
          busy={remove.isPending}
          onClose={removals.clear}
          onConfirm={(name) => remove.mutateAsync({ name, force: true })
            .then(() => { removals.clear(); return apply(); })
            .catch((error) => toast.err(String(error)))}
        />
        <LibraryFooter
          changeCount={library.changeCount}
          onApply={() => apply()}
          onApplyDefault={scope === "project" ? () => apply(true) : undefined}
          onDiscard={library.discard}
          busy={applying}
        />
      </footer>
    </div>
  );
}
