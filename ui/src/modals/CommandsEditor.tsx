/** The allowlists: which programs an agent may run.
 *
 * Named lists rather than one global set, because a tester needs npx and jest,
 * devops needs npm and docker, and a reviewer needs neither — one list meant
 * everyone shared the union of everything anyone ever needed.
 */

import { useEffect, useState } from "react";
import { useCommands } from "@/api/queries";
import { useCommandMutations } from "@/api/mutations";
import { cn } from "@/lib/cn";
import { Badge, Button, Checkbox, Empty, Field, Textarea } from "@/components/ui/primitives";
import { toast } from "@/components/Toaster";

export function CommandsEditor() {
  const commands = useCommands();
  const { save, remove, reset } = useCommandMutations();
  const [selected, setSelected] = useState("default");
  const [allowed, setAllowed] = useState("");
  const [shell, setShell] = useState(true);

  const list = commands.data?.lists[selected];

  useEffect(() => {
    if (!list) return;
    setAllowed(list.allowed.join(" "));
    setShell(list.shell);
  }, [list, selected]);

  if (!commands.data) return <Empty title="Loading…" />;

  const names = commands.data.names ?? Object.keys(commands.data.lists);
  const shipped = new Set(commands.data.defaults ?? []);
  const programs = allowed.split(/\s+/).filter(Boolean);
  const added = programs.filter((program) => !shipped.has(program));

  return (
    <div className="grid h-[60vh] grid-cols-[12rem_1fr]">
      <aside className="min-h-0 space-y-0.5 overflow-y-auto border-r border-line p-2">
        {names.map((name) => (
          <button
            key={name}
            onClick={() => setSelected(name)}
            className={cn(
              "block w-full truncate rounded-[--radius] px-2 py-1.5 text-left text-sm",
              "transition-colors hover:bg-panel-2",
              name === selected && "bg-panel-2 ring-1 ring-accent/40",
            )}
          >
            {name}
            {commands.data!.lists[name] && (
              <span className="ml-1 text-[10px] text-muted">
                {commands.data!.lists[name]!.allowed.length}
              </span>
            )}
          </button>
        ))}
      </aside>

      <div className="min-h-0 space-y-4 overflow-y-auto p-4">
        <Field
          label={`Programs in "${selected}"`}
          hint="Space separated, program names only — no paths and no arguments. Every program in a piped line is checked against this list."
        >
          <Textarea rows={8} className="font-code" value={allowed}
                    onChange={(event) => setAllowed(event.target.value)} />
        </Field>

        {added.length > 0 && (
          <p className="text-xs text-muted">
            Beyond what trance ships with:{" "}
            <span className="text-accent">{added.join(", ")}</span>
          </p>
        )}

        <Checkbox
          label="Allow pipes, redirects and &&"
          hint="Off means one plain command per call. Every program in the line is checked either way."
          checked={shell}
          onChange={(event) => setShell(event.target.checked)}
        />

        {commands.data.overrides && Object.keys(commands.data.overrides).length > 0 && (
          <div className="space-y-1">
            <div className="text-xs font-medium text-muted">Agents using their own list</div>
            <div className="flex flex-wrap gap-1.5">
              {Object.entries(commands.data.overrides).map(([agent, name]) => (
                <Badge key={agent} tone={name === selected ? "accent" : "neutral"}>
                  {agent} → {name || "default"}
                </Badge>
              ))}
            </div>
          </div>
        )}

        <div className="flex items-center gap-2 border-t border-line pt-3">
          <Button
            variant="primary" busy={save.isPending}
            onClick={() => save.mutateAsync({ name: selected, body: { allowed: programs, shell } })
              .then(() => toast.ok(`Saved "${selected}".`))
              .catch((error) => toast.err(String(error)))}
          >Save</Button>
          <Button
            busy={reset.isPending}
            onClick={() => { if (confirm("Restore the shipped allowlists?")) {
              reset.mutateAsync().then(() => toast.ok("Restored."))
                .catch((error) => toast.err(String(error)));
            } }}
          >Reset to shipped</Button>
          {selected !== "default" && (
            <Button
              variant="danger" busy={remove.isPending}
              onClick={() => { if (confirm(`Delete the list "${selected}"?`)) {
                remove.mutateAsync(selected)
                  .then(() => { setSelected("default"); toast.ok("Deleted."); })
                  .catch((error) => toast.err(String(error)));
              } }}
            >Delete</Button>
          )}
        </div>
      </div>
    </div>
  );
}
