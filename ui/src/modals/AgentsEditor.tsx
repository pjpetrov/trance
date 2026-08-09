/** A library of agents: the list on the left, the one you are editing on the right.
 *
 * The browser toolset is the only one whose usefulness depends on the machine
 * and on which model the agent runs, so it says so where the switch is rather
 * than at run time, a step later.
 */

import { useEffect, useState } from "react";
import { useAgents, useConfig, usePresets } from "@/api/queries";
import { useAgentMutations } from "@/api/mutations";
import { cn } from "@/lib/cn";
import { Badge, Button, Checkbox, Empty, Field, Input, Select, Textarea }
  from "@/components/ui/primitives";
import { toast } from "@/components/Toaster";
import type { AgentRole, Toolset } from "@/api/types";

const TOOLSET_HELP: Record<string, string> = {
  files: "read / write / list files inside the remit",
  graph: "look up symbols, callers and callees in the indexed code",
  commands: "run allowlisted commands (pytest, npm, tsc, …)",
  inspect: "check a file exists and has content — never its contents",
  browser: "open the app in a real browser, press keys, and look at what is on screen "
    + "with a vision model. The only way to check a canvas.",
};

export function AgentsEditor() {
  const agents = useAgents();
  const presets = usePresets();
  const config = useConfig();
  const { save, reset, remove } = useAgentMutations();

  const list = Object.values(agents.data?.byName ?? {});
  const [selected, setSelected] = useState<string>("");
  const [draft, setDraft] = useState<AgentRole | null>(null);

  useEffect(() => {
    if (!list.length) return;
    const name = list.some((role) => role.name === selected) ? selected : list[0]!.name;
    if (name !== selected) setSelected(name);
    setDraft(agents.data?.byName[name] ?? null);
  }, [agents.data, selected, list.length]);

  if (!list.length) return <Empty title="Loading agents…" />;

  const edit = (patch: Partial<AgentRole>) =>
    setDraft((current) => (current ? { ...current, ...patch } : current));

  const browserOn = draft?.toolsets.includes("browser");

  return (
    <div className="grid h-[70vh] grid-cols-[14rem_1fr]">
      <aside className="min-h-0 space-y-0.5 overflow-y-auto border-r border-line p-2">
        {list.map((role) => (
          <button
            key={role.name}
            onClick={() => setSelected(role.name)}
            className={cn(
              "flex w-full items-center gap-2 rounded-[--radius] px-2 py-1.5 text-left text-sm",
              "transition-colors hover:bg-panel-2",
              role.name === selected && "bg-panel-2 ring-1 ring-accent/40",
            )}
          >
            <span className="size-2 shrink-0 rounded-full"
                  style={{ background: role.color }} />
            <span className="min-w-0 flex-1 truncate">{role.name}</span>
            {role.verifier && <span className="text-[10px] text-muted">verifier</span>}
          </button>
        ))}
      </aside>

      {draft && (
        <div className="min-h-0 space-y-4 overflow-y-auto p-4">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Title"><Input value={draft.title}
              onChange={(e) => edit({ title: e.target.value })} /></Field>
            <Field label="Model">
              <Select value={draft.preset ?? ""}
                      onChange={(e) => edit({ preset: e.target.value || null })}>
                <option value="">— the default —</option>
                {presets.data?.map((preset) => (
                  <option key={preset.name} value={preset.name}>{preset.name}</option>
                ))}
              </Select>
            </Field>
          </div>

          <Field label="Description" hint="The orchestrator reads this when assigning work.">
            <Input value={draft.description} onChange={(e) => edit({ description: e.target.value })} />
          </Field>

          <Field label="Tool rounds"
                 hint="Reads, writes or commands per attempt before it must report. 0 uses the default of 12.">
            <Input type="number" min={0} value={draft.tool_rounds || ""}
                   onChange={(e) => edit({ tool_rounds: Number(e.target.value) || 0 })} />
          </Field>

          <section className="space-y-1">
            <div className="text-xs font-medium text-muted">Capabilities</div>
            <div className="flex flex-wrap gap-x-5">
              {(["files", "graph", "commands", "inspect", "browser"] as Toolset[]).map((tool) => (
                <Checkbox
                  key={tool}
                  label={tool}
                  title={TOOLSET_HELP[tool]}
                  checked={draft.toolsets.includes(tool)}
                  onChange={(event) => edit({
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
                      draft.preset ? ` (${draft.preset})` : ""}, which must be able to see
                      images. The app is served as static files — no build or dev server
                      is started.`
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
              onChange={(e) => edit({
                paths: e.target.value.split("\n").map((p) => p.trim()).filter(Boolean),
              })} />
          </Field>

          <Checkbox
            label="Can verify other steps"
            hint="Only agents that can actually inspect the result should be — one with no tools would return a verdict it had no way to have checked."
            checked={draft.verifier}
            onChange={(event) => edit({ verifier: event.target.checked })}
          />

          <Field label="System prompt">
            <Textarea rows={12} className="font-code" value={draft.system_prompt}
                      onChange={(e) => edit({ system_prompt: e.target.value })} />
          </Field>

          <div className="flex items-center gap-2 border-t border-line pt-3">
            <Button variant="primary" busy={save.isPending}
              onClick={() => save.mutateAsync({ name: draft.name, body: draft })
                .then(() => toast.ok(`Saved ${draft.name}.`))
                .catch((error) => toast.err(String(error)))}>
              Save
            </Button>
            {draft.protected
              ? (
                <Button busy={reset.isPending}
                  onClick={() => reset.mutateAsync(draft.name)
                    .then(() => toast.ok("Restored the shipped definition."))
                    .catch((error) => toast.err(String(error)))}>
                  Reset to default
                </Button>
              )
              : (
                <Button variant="danger" busy={remove.isPending}
                  onClick={() => { if (confirm(`Delete ${draft.name}?`)) {
                    remove.mutateAsync(draft.name).catch((error) => toast.err(String(error)));
                  } }}>
                  Delete
                </Button>
              )}
            <div className="flex-1" />
            {draft.resolved && <Badge>{draft.resolved.model}</Badge>}
          </div>
        </div>
      )}
    </div>
  );
}
