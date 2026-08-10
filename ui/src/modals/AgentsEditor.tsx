/** A library of agents: the list on the left, the one you are editing on the
 *  right, and one Apply for everything you changed.
 *
 *  The browser toolset is the only one whose usefulness depends on the machine
 *  and on which model the agent runs, so it says so where the switch is rather
 *  than at run time, a step later.
 */

import { useState } from "react";
import { useAgents, useConfig, usePresets } from "@/api/queries";
import { useAgentMutations } from "@/api/mutations";
import { useDraftLibrary } from "@/lib/useDraftLibrary";
import { useUi } from "@/store/ui";
import { cn } from "@/lib/cn";
import { Badge, Button, Checkbox, Empty, Field, Input, Select, Textarea }
  from "@/components/ui/primitives";
import { LibraryFooter, LibraryList } from "@/components/ui/Library";
import { Checks } from "@/components/Checks";
import { DEFAULTS, ScopeSwitch, idFor, type Scope }
  from "@/components/ScopeSwitch";
import { toast } from "@/components/Toaster";
import type { AgentRole, Toolset } from "@/api/types";

const TOOLSETS: Toolset[] = ["files", "graph", "commands", "inspect", "browser"];

const TOOLSET_HELP: Record<Toolset, string> = {
  files: "read / write / list files inside the remit",
  graph: "look up symbols, callers and callees in the indexed code",
  commands: "run allowlisted commands (pytest, npm, tsc, …)",
  inspect: "check a file exists and has content — never its contents",
  browser: "open the app in a real browser, press keys, and look at what is on screen "
    + "with a vision model. The only way to check a canvas.",
};

function blankAgent(taken: Set<string>): AgentRole {
  let name = "new-agent";
  for (let n = 2; taken.has(name); n += 1) name = `new-agent-${n}`;
  return {
    name, title: "New agent", description: "", system_prompt: "",
    paths: [], toolsets: ["files", "graph"], commands: [], command_list: "",
    workdir: "", shell: null, verifier: false, preset: null, backup_preset: null,
    tries: 2, backup_tries: 2, tool_rounds: 0, color: "#7aa2f7",
  };
}

export function AgentsEditor() {
  const session = useUi((state) => state.sessionId);
  const [scope, setScope] = useState<Scope>("project");
  const sessionId = idFor(scope, session);
  const agents = useAgents(sessionId);
  const presets = usePresets();
  const config = useConfig();
  const mine = useAgentMutations(sessionId);
  const { save, remove, reset, draftPrompt } = mine;
  // The same writes, aimed at what new sessions are created from.
  const template = useAgentMutations(DEFAULTS);
  const [applying, setApplying] = useState(false);

  const library = useDraftLibrary<AgentRole>(agents.data?.agents, (role) => role.name);
  const draft = library.selected;

  const apply = async (alsoDefault = false) => {
    setApplying(true);
    try {
      // Deletions first: a rename is expressed as an add plus a delete, and
      // doing them the other way round briefly has both names present.
      for (const name of library.removed) await remove.mutateAsync(name);
      for (const role of library.changed) {
        await save.mutateAsync({ name: role.name, body: role });
        // Only what this edit wrote. A deletion is not repeated against the
        // template: an agent still on some other session's team cannot be
        // deleted there, and half of a two-part apply failing is worse than
        // not offering it. Delete it in the Default scope.
        if (alsoDefault) {
          await template.save.mutateAsync({ name: role.name, body: role });
        }
      }
      library.settle();
      toast.ok(`Applied ${library.changeCount} change(s)`
               + (alsoDefault ? ", here and for new sessions." : "."));
    } catch (error) {
      // The draft is deliberately kept: some of it may have saved, and throwing
      // away the rest because one failed would lose work.
      toast.err(`${error}. Nothing else was applied.`);
    } finally {
      setApplying(false);
    }
  };

  if (agents.isLoading) return <Empty title="Loading agents…" />;
  const browserOn = draft?.toolsets.includes("browser");
  // The preset this agent would run delegated — the main or the backup.
  const delegatedPreset = [draft?.preset, draft?.backup_preset].find((name) =>
    name && presets.data?.some((held) =>
      held.name === name && held.kind === "claudecode"));
  const isNew = draft ? library.isNew(draft.name) : false;

  return (
    <div className="flex h-[70vh] flex-col">
      <div className="grid min-h-0 flex-1 grid-cols-[14rem_minmax(0,1fr)]">
        <LibraryList
          items={library.items}
          nameOf={(role) => role.name}
          selected={library.selectedName}
          onSelect={library.select}
          isNew={library.isNew}
          removed={library.removed}
          addLabel="New agent"
          onAdd={() => library.add(
            blankAgent(new Set(library.items.map((role) => role.name))))}
          meta={(role) => (
            <span className="size-2 shrink-0 rounded-full" style={{ background: role.color }} />
          )}
        />

        {draft && (
          <div className="min-h-0 space-y-4 overflow-y-auto p-4">
            {/* Who it is, then what it runs on and what happens when that
                fails — read down the left column and it is one sentence. */}
            <div className="grid grid-cols-2 gap-3">
              <Field
                label="Name"
                hint={isNew ? "Flows reference agents by name; it cannot change later."
                            : "Fixed — flows reference this agent by name."}
              >
                <Input
                  value={draft.name} disabled={!isNew}
                  onChange={(event) => library.replace({ ...draft, name: event.target.value })}
                />
              </Field>
              <Field label="Title">
                <Input value={draft.title}
                       onChange={(e) => library.edit({ title: e.target.value })} />
              </Field>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <Field label="Model">
                <Select value={draft.preset ?? ""}
                        onChange={(e) => library.edit({ preset: e.target.value || null })}>
                  <option value="">— the default —</option>
                  {presets.data?.map((preset) => (
                    <option key={preset.name} value={preset.name}>{preset.name}</option>
                  ))}
                </Select>
              </Field>
              <Field label="Retries" hint="Attempts on that model before the backup takes over.">
                <Input type="number" min={1} value={draft.tries || 1}
                       onChange={(e) => library.edit({ tries: Number(e.target.value) || 1 })} />
              </Field>
            </div>

            {delegatedPreset && (
              // Not a blocker — the trade can be worth it — but the person
              // making it should know it is a trade. Every other backend's
              // writes are refused at the remit as they happen; this one's
              // are judged from the git diff after the step finishes.
              <p className="rounded-[--radius] border border-warn/40 bg-warn/10
                            px-3 py-2 text-xs text-warn">
                {delegatedPreset} runs steps inside Claude Code, with its own
                tools. Control is checked after the fact, not enforced live:
                writes outside the remit fail the step from the diff once it
                ends, and commands run under Claude Code's permission rules
                shaped from the allowlist. Cheapest for small, well-scoped
                steps and reviews — every internal turn re-sends the whole
                conversation, and a retry pays the entire step again.
              </p>
            )}

            <div className="grid grid-cols-2 gap-3">
              <Field
                label="Backup model"
                hint={draft.backup_preset
                  ? "Used once the retries above are spent — the one thing a retry otherwise never changes."
                  : "None — it retries on the same model, which usually fails the same way."}
              >
                <Select value={draft.backup_preset ?? ""}
                        onChange={(e) => library.edit({ backup_preset: e.target.value || null })}>
                  <option value="">— no backup —</option>
                  {presets.data?.filter((preset) => preset.name !== draft.preset)
                    .map((preset) => (
                      <option key={preset.name} value={preset.name}>{preset.name}</option>
                    ))}
                </Select>
              </Field>
              <Field
                label="Backup retries"
                hint={draft.backup_preset
                  ? "Attempts on the backup after that."
                  : "Ignored without a backup model — the agent simply gets the retries above."}
              >
                <Input type="number" min={0} disabled={!draft.backup_preset}
                       value={draft.backup_tries ?? 0}
                       onChange={(e) => library.edit({
                         backup_tries: Number(e.target.value) || 0 })} />
              </Field>
            </div>

            <Field
              label="Tool rounds"
              hint="Reads, writes or commands per attempt before it must report. 0 uses the default."
            >
              <Input type="number" min={0} className="w-28" value={draft.tool_rounds || ""}
                     onChange={(e) => library.edit({ tool_rounds: Number(e.target.value) || 0 })} />
            </Field>

            <section className="space-y-1">
              <div className="text-xs font-medium text-muted">Capabilities</div>
              <div className="flex flex-wrap gap-x-5">
                {TOOLSETS.map((tool) => (
                  <Checkbox
                    key={tool}
                    label={tool}
                    title={TOOLSET_HELP[tool]}
                    checked={draft.toolsets.includes(tool)}
                    onChange={(event) => library.edit({
                      toolsets: event.target.checked
                        ? [...draft.toolsets, tool]
                        : draft.toolsets.filter((held) => held !== tool),
                    })}
                  />
                ))}
              </div>
              {browserOn && (
                <p className={cn("text-xs leading-snug",
                  config.data?.visual.browser ? "text-muted" : "text-warn")}>
                  {config.data?.visual.browser
                    ? `Browser found · screenshots go to this agent's own model${
                        draft.preset ? ` (${draft.preset})` : ""}, which must be able to see `
                      + "images. The app is served as static files — no build or dev server "
                      + "is started."
                    : "No Chrome or Chromium on this machine — this agent's browser tools will "
                      + "refuse and say so. Install one, or leave this off."}
                </p>
              )}
            </section>

            <Field
              label="Remit"
              hint={draft.paths.length
                ? `${draft.paths.length} glob(s). A write outside them is refused by the system.`
                : "Empty means read-only, which is a choice: a reviewer that must not touch the code is exactly this."}
            >
              <Textarea rows={3} className="font-code" value={draft.paths.join("\n")}
                onChange={(e) => library.edit({
                  paths: e.target.value.split("\n").map((p) => p.trim()).filter(Boolean),
                })} />
            </Field>

            {/* What this agent always wants proved about its own work. Set
                once here rather than ticked onto each of twenty steps: "after
                each step, check nothing broke" is a property of the work, and
                a check added by hand per step stops being added. */}
            <Checks
              label="Always checked by"
              empty="nothing — only whatever the plan puts on each step"
              checks={draft.checks ?? []}
              verifiers={(agents.data?.agents ?? [])
                .filter((role) => role.verifier && role.name !== draft.name)}
              onChange={(checks) => library.edit({ checks })}
            />

            <Checkbox
              label="Can verify other steps"
              hint="Only agents that can actually inspect the result should be — one with no tools would return a verdict it had no way to have checked."
              checked={draft.verifier}
              onChange={(event) => library.edit({ verifier: event.target.checked })}
            />

            <Field
              label="Description"
              hint="One line. The orchestrator reads exactly this when deciding which agent gets a step — it never sees the system prompt below."
            >
              <Input value={draft.description}
                     onChange={(e) => library.edit({ description: e.target.value })} />
            </Field>

            <Field label="System prompt">
              <Textarea rows={12} className="font-code" value={draft.system_prompt}
                        onChange={(e) => library.edit({ system_prompt: e.target.value })} />
            </Field>

            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm" busy={draftPrompt.isPending}
                onClick={() => draftPrompt.mutateAsync({
                  name: draft.name, title: draft.title, description: draft.description,
                })
                  .then((result) => library.edit({
                    system_prompt: result.prompt,
                    description: result.description ?? draft.description,
                  }))
                  .catch((error) => toast.err(String(error)))}
              >Draft a prompt from the name</Button>
              {draft.protected && (
                // A session keeps its own copy of every prompt, taken when it
                // was created. Improvements to a shipped agent otherwise reach
                // new sessions only, and the project you are actually running
                // keeps the prompt it was born with.
                <Button
                  size="sm" busy={reset.isPending}
                  title="Take the current shipped prompt and permissions for this agent"
                  onClick={() => reset.mutateAsync(draft.name)
                    .then(() => {
                      // The server has already written it; anything staged for
                      // this agent would only put the old prompt back on Apply.
                      library.forget(draft.name);
                      toast.ok(`${draft.name} is back to its shipped prompt.`);
                    })
                    .catch((error) => toast.err(String(error)))}
                >Restore shipped prompt</Button>
              )}
              <Button
                size="sm" variant="danger"
                onClick={() => library.remove(draft.name)}
              >Delete</Button>
              <div className="flex-1" />
              {draft.protected && <Badge>built-in</Badge>}
              {draft.resolved && <Badge>{draft.resolved.model}</Badge>}
            </div>
          </div>
        )}
      </div>

      <footer className="flex items-center gap-2 border-t border-line px-4 py-3">
        <ScopeSwitch scope={scope} onChange={setScope} what="agents" />
        <div className="flex-1" />
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
