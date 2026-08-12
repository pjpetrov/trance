/** The models, edited like every other library: add on the left, one Apply.
 *
 * A model carries its own endpoint, so an agent picks one thing rather than a
 * provider and a model id that have to be kept in step.
 */

import { useState } from "react";
import { useConfig, usePresets } from "@/api/queries";
import { usePresetMutations } from "@/api/mutations";
import { useDraftLibrary } from "@/lib/useDraftLibrary";
import { tokens } from "@/lib/format";
import { Badge, Button, Empty, Field, Input, Select } from "@/components/ui/primitives";
import { ForceConfirm, LibraryFooter, LibraryList, useForcedRemoval }
  from "@/components/ui/Library";
import { toast } from "@/components/Toaster";
import type { ModelPreset } from "@/api/types";

function blankPreset(taken: Set<string>, kind: string, base_url: string,
                     context_window: number): ModelPreset {
  let name = "new-model";
  for (let n = 2; taken.has(name); n += 1) name = `new-model-${n}`;
  return {
    name, kind, model: "", base_url, api_key: null, context_window,
    max_tokens: 0, has_key: false, self_contained: true,
  };
}

export function ModelsEditor() {
  const presets = usePresets();
  const config = useConfig();
  const { save, remove, check, discover } = usePresetMutations();
  const [applying, setApplying] = useState(false);

  const library = useDraftLibrary<ModelPreset>(presets.data, (preset) => preset.name);
  const draft = library.selected;
  const kinds = config.data?.kinds ?? {};

  const removals = useForcedRemoval(
    (name, force) => remove.mutateAsync({ name, force }));

  const apply = async () => {
    setApplying(true);
    try {
      if (!(await removals.removeAll(library.removed))) return;
      for (const preset of library.changed) {
        // "***" is the redaction the server sends back; echoing it would store
        // the placeholder as the key.
        const body = preset.api_key === "***" ? { ...preset, api_key: undefined } : preset;
        await save.mutateAsync({ name: preset.name, body });
      }
      library.settle();
      toast.ok(`Applied ${library.changeCount} change(s).`);
    } catch (error) {
      toast.err(`${error}. Nothing else was applied.`);
    } finally {
      setApplying(false);
    }
  };

  if (presets.isLoading) return <Empty title="Loading models…" />;
  const isNew = draft ? library.isNew(draft.name) : false;
  const kind = draft ? kinds[draft.kind] : undefined;

  return (
    <div className="flex h-[70vh] flex-col">
      <div className="grid min-h-0 flex-1 grid-cols-[14rem_minmax(0,1fr)]">
        <LibraryList
          items={library.items}
          nameOf={(preset) => preset.name}
          selected={library.selectedName}
          onSelect={library.select}
          isNew={library.isNew}
          removed={library.removed}
          addLabel="New model"
          onAdd={() => {
            const first = Object.entries(kinds)[0];
            library.add(blankPreset(
              new Set(library.items.map((preset) => preset.name)),
              first?.[0] ?? "llamacpp",
              first?.[1]?.base_url ?? "",
              first?.[1]?.context_window ?? 64000));
          }}
          meta={(preset) => (preset.spend
            ? <span className="shrink-0 text-[10px] text-muted">
                {tokens(preset.spend.total)}
              </span>
            : null)}
        />

        {draft && (
          <div className="min-h-0 space-y-3 overflow-y-auto p-4">
            <div className="grid grid-cols-2 gap-3">
              <Field
                label="Name"
                hint={isNew ? "Agents pick a model by this name."
                            : "Renaming is a separate action — agents point at this name."}
              >
                <Input value={draft.name} disabled={!isNew}
                       onChange={(e) => library.replace({ ...draft, name: e.target.value })} />
              </Field>
              <Field label="Kind" hint={kind?.label}>
                <Select
                  value={draft.kind} disabled={!isNew}
                  onChange={(event) => {
                    const next = kinds[event.target.value];
                    library.edit({
                      kind: event.target.value,
                      base_url: next?.base_url ?? draft.base_url,
                      context_window: next?.context_window ?? draft.context_window,
                    });
                  }}
                >
                  {Object.entries(kinds).map(([id, meta]) => (
                    <option key={id} value={id}>{meta.label}</option>
                  ))}
                </Select>
              </Field>
            </div>

            <Field label="Model id">
              <div className="flex gap-2">
                <Input value={draft.model}
                       onChange={(e) => library.edit({ model: e.target.value })} />
                <Button
                  size="sm" busy={discover.isPending}
                  title="Ask the endpoint what it serves"
                  onClick={() => discover.mutateAsync({
                    kind: draft.kind, base_url: draft.base_url, api_key: draft.api_key,
                  })
                    .then((result) => {
                      if (result.error) return toast.err(result.error);
                      if (!result.models.length) return toast.info("It listed no models.");
                      library.edit({ model: result.models[0] });
                      toast.ok(`${result.models.length} available; took the first.`);
                    })
                    .catch((error) => toast.err(String(error)))}
                >discover</Button>
              </div>
            </Field>

            <Field label="Endpoint" hint={kind?.needs_key ? "Needs an API key." : undefined}>
              <Input value={draft.base_url}
                     onChange={(e) => library.edit({ base_url: e.target.value })} />
            </Field>

            <div className="grid grid-cols-2 gap-3">
              <Field label="Context window">
                <Input type="number" value={draft.context_window}
                       onChange={(e) => library.edit({
                         context_window: Number(e.target.value) || 0 })} />
              </Field>
              <Field label="Max output tokens" hint="0 scales with the window.">
                <Input type="number" value={draft.max_tokens}
                       onChange={(e) => library.edit({
                         max_tokens: Number(e.target.value) || 0 })} />
              </Field>
            </div>

            <label className="flex items-center gap-2 text-sm"
                   title="The model an agent falls back to when its own is deleted or missing — instead of whichever model happens to be first in the file">
              <input
                type="checkbox"
                checked={Boolean(draft.default) || (presets.data?.length ?? 0) === 1}
                disabled={(presets.data?.length ?? 0) <= 1}
                onChange={(e) => library.edit({ default: e.target.checked })}
              />
              <span>Default model</span>
              <span className="text-xs text-muted">
                {(presets.data?.length ?? 0) <= 1
                  ? "— the only model, so obviously the fallback"
                  : "— the fallback for any agent whose own model is missing"}
              </span>
            </label>

            <Field label="API key"
                   hint={draft.has_key ? "A key is stored. Leave blank to keep it."
                                       : "No key stored."}>
              <Input type="password" placeholder={draft.has_key ? "••••••" : "none"}
                     onChange={(e) => library.edit({ api_key: e.target.value })} />
            </Field>

            {draft.spend && (
              <div className="flex gap-3 text-xs text-muted">
                <span>{draft.spend.calls} calls</span>
                <span>{tokens(draft.spend.input_tokens)} in</span>
                <span>{tokens(draft.spend.output_tokens)} out</span>
              </div>
            )}

            <div className="flex items-center gap-2">
              <Button
                size="sm" busy={check.isPending} disabled={library.dirty}
                title={library.dirty ? "Apply your changes first — this tests what is saved"
                                     : "Send one request and report what came back"}
                onClick={() => check.mutateAsync(draft.name)
                  .then((result) => result.ok
                    ? toast.ok(`${draft.name} answered in ${result.took_ms ?? "?"}ms.`)
                    : toast.err(result.error ?? "no answer"))
                  .catch((error) => toast.err(String(error)))}
              >Test</Button>
              <Button size="sm" variant="danger" onClick={() => library.remove(draft.name)}>
                Delete
              </Button>
              <div className="flex-1" />
              <Badge tone={draft.self_contained ? "ok" : "neutral"}>
                {draft.self_contained ? "self-contained" : "borrows a provider"}
              </Badge>
            </div>
          </div>
        )}
      </div>

      <footer className="flex items-center gap-2 border-t border-line px-4 py-3">
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
          onApply={apply}
          onDiscard={library.discard}
          busy={applying}
        />
      </footer>
    </div>
  );
}
