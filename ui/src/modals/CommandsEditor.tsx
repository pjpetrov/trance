/** The allowlists: which programs an agent may run.
 *
 * Named lists rather than one global set, because a tester needs npx and jest,
 * devops needs npm and docker, and a reviewer needs neither — one list meant
 * everyone shared the union of everything anyone ever needed.
 */

import { useState } from "react";
import { useCommands } from "@/api/queries";
import { useCommandMutations } from "@/api/mutations";
import { useDraftLibrary } from "@/lib/useDraftLibrary";
import { useUi } from "@/store/ui";
import { Badge, Button, Checkbox, Empty, Field, Input, Textarea }
  from "@/components/ui/primitives";
import { LibraryFooter, LibraryList } from "@/components/ui/Library";
import { toast } from "@/components/Toaster";

/** A list as the editor holds it: the API keys lists by name, which a draft
 *  library cannot address, so the name rides along. */
interface NamedList {
  name: string;
  allowed: string[];
  shell: boolean;
}

export function CommandsEditor() {
  const sessionId = useUi((state) => state.sessionId) ?? "";
  const commands = useCommands(sessionId);
  const { save, remove, reset } = useCommandMutations(sessionId);
  const [applying, setApplying] = useState(false);

  const lists: NamedList[] = Object.entries(commands.data?.lists ?? {})
    .map(([name, policy]) => ({ name, allowed: policy.allowed, shell: policy.shell }));

  const library = useDraftLibrary<NamedList>(
    commands.data ? lists : undefined, (list) => list.name);
  const draft = library.selected;

  const apply = async () => {
    setApplying(true);
    try {
      for (const name of library.removed) await remove.mutateAsync(name);
      for (const list of library.changed) {
        await save.mutateAsync({
          name: list.name, body: { allowed: list.allowed, shell: list.shell },
        });
      }
      library.settle();
      toast.ok(`Applied ${library.changeCount} change(s).`);
    } catch (error) {
      toast.err(`${error}. Nothing else was applied.`);
    } finally {
      setApplying(false);
    }
  };

  if (commands.isLoading) return <Empty title="Loading…" />;

  const shipped = new Set(commands.data?.defaults ?? []);
  const added = (draft?.allowed ?? []).filter((program) => !shipped.has(program));
  const isNew = draft ? library.isNew(draft.name) : false;

  return (
    <div className="flex h-[60vh] flex-col">
      <div className="grid min-h-0 flex-1 grid-cols-[13rem_minmax(0,1fr)]">
        <LibraryList
          items={library.items}
          nameOf={(list) => list.name}
          selected={library.selectedName}
          onSelect={library.select}
          isNew={library.isNew}
          removed={library.removed}
          addLabel="New list"
          onAdd={() => {
            const taken = new Set(library.items.map((list) => list.name));
            let name = "new-list";
            for (let n = 2; taken.has(name); n += 1) name = `new-list-${n}`;
            // Seeded from the default rather than empty: an agent pointed at an
            // empty list can run nothing at all, which reads as a broken agent.
            library.add({
              name,
              allowed: [...(commands.data?.lists.default?.allowed ?? [])],
              shell: commands.data?.lists.default?.shell ?? true,
            });
          }}
          meta={(list) => (
            <span className="shrink-0 text-[10px] text-muted">{list.allowed.length}</span>
          )}
        />

        {draft && (
          <div className="min-h-0 space-y-4 overflow-y-auto p-4">
            <Field
              label="Name"
              hint={isNew ? "Agents point at a list by name."
                          : "Fixed — agents point at this list by name."}
            >
              <Input value={draft.name} disabled={!isNew || draft.name === "default"}
                     onChange={(e) => library.replace({ ...draft, name: e.target.value })} />
            </Field>

            <Field
              label="Programs"
              hint="Space separated, program names only — no paths and no arguments. Every program in a piped line is checked against this list."
            >
              <Textarea
                rows={8} className="font-code" value={draft.allowed.join(" ")}
                onChange={(event) => library.edit({
                  allowed: event.target.value.split(/\s+/).filter(Boolean),
                })}
              />
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
              checked={draft.shell}
              onChange={(event) => library.edit({ shell: event.target.checked })}
            />

            {commands.data?.overrides && Object.keys(commands.data.overrides).length > 0 && (
              <div className="space-y-1">
                <div className="text-xs font-medium text-muted">Agents using their own list</div>
                <div className="flex flex-wrap gap-1.5">
                  {Object.entries(commands.data.overrides).map(([agent, name]) => (
                    <Badge key={agent} tone={name === draft.name ? "accent" : "neutral"}>
                      {agent} → {name || "default"}
                    </Badge>
                  ))}
                </div>
              </div>
            )}

            <div className="flex gap-2">
              {draft.name !== "default" && (
                <Button size="sm" variant="danger" onClick={() => library.remove(draft.name)}>
                  Delete this list
                </Button>
              )}
              <Button
                size="sm" busy={reset.isPending}
                onClick={() => { if (confirm("Restore the shipped allowlists? Pending changes are discarded.")) {
                  library.discard();
                  reset.mutateAsync().then(() => toast.ok("Restored."))
                    .catch((error) => toast.err(String(error)));
                } }}
              >Reset to shipped</Button>
            </div>
          </div>
        )}
      </div>

      <footer className="flex items-center gap-2 border-t border-line px-4 py-3">
        <LibraryFooter
          changeCount={library.changeCount}
          onApply={apply}
          onDiscard={library.discard}
          busy={applying}
        />
      </footer>
    </div>
  );
}
