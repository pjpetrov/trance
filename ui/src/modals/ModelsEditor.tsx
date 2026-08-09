import { useEffect, useState } from "react";
import { usePresets } from "@/api/queries";
import { usePresetMutations } from "@/api/mutations";
import { cn } from "@/lib/cn";
import { tokens } from "@/lib/format";
import { Badge, Button, Empty, Field, Input } from "@/components/ui/primitives";
import { toast } from "@/components/Toaster";
import type { ModelPreset } from "@/api/types";

export function ModelsEditor() {
  const presets = usePresets();
  const { save, check, remove } = usePresetMutations();
  const [selected, setSelected] = useState("");
  const [draft, setDraft] = useState<ModelPreset | null>(null);

  useEffect(() => {
    const list = presets.data ?? [];
    if (!list.length) return;
    const found = list.find((preset) => preset.name === selected) ?? list[0]!;
    if (found.name !== selected) setSelected(found.name);
    setDraft(found);
  }, [presets.data, selected]);

  if (!presets.data?.length) return <Empty title="No models configured." />;

  const edit = (patch: Partial<ModelPreset>) =>
    setDraft((current) => (current ? { ...current, ...patch } : current));

  return (
    <div className="grid h-[70vh] grid-cols-[14rem_1fr]">
      <aside className="min-h-0 space-y-0.5 overflow-y-auto border-r border-line p-2">
        {presets.data.map((preset) => (
          <button
            key={preset.name}
            onClick={() => setSelected(preset.name)}
            className={cn(
              "flex w-full items-center gap-2 rounded-[--radius] px-2 py-1.5 text-left text-sm",
              "transition-colors hover:bg-panel-2",
              preset.name === selected && "bg-panel-2 ring-1 ring-accent/40",
            )}
          >
            <span className="min-w-0 flex-1 truncate">{preset.name}</span>
            {preset.spend && (
              <span className="text-[10px] text-muted">{tokens(preset.spend.total)}</span>
            )}
          </button>
        ))}
      </aside>

      {draft && (
        <div className="min-h-0 space-y-3 overflow-y-auto p-4">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Kind"><Input value={draft.kind} readOnly /></Field>
            <Field label="Model id">
              <Input value={draft.model} onChange={(e) => edit({ model: e.target.value })} />
            </Field>
          </div>
          <Field label="Endpoint">
            <Input value={draft.base_url} onChange={(e) => edit({ base_url: e.target.value })} />
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Context window">
              <Input type="number" value={draft.context_window}
                     onChange={(e) => edit({ context_window: Number(e.target.value) })} />
            </Field>
            <Field label="Max output tokens" hint="0 scales with the window.">
              <Input type="number" value={draft.max_tokens}
                     onChange={(e) => edit({ max_tokens: Number(e.target.value) })} />
            </Field>
          </div>
          <Field label="API key" hint={draft.has_key ? "A key is stored." : "No key stored."}>
            <Input type="password" placeholder={draft.has_key ? "••••••" : "none"}
                   onChange={(e) => edit({ api_key: e.target.value })} />
          </Field>

          {draft.spend && (
            <div className="flex gap-3 text-xs text-muted">
              <span>{draft.spend.calls} calls</span>
              <span>{tokens(draft.spend.input_tokens)} in</span>
              <span>{tokens(draft.spend.output_tokens)} out</span>
            </div>
          )}

          <div className="flex items-center gap-2 border-t border-line pt-3">
            <Button variant="primary" busy={save.isPending}
              onClick={() => save.mutateAsync({ name: draft.name, body: draft })
                .then(() => toast.ok(`Saved ${draft.name}.`))
                .catch((error) => toast.err(String(error)))}>Save</Button>
            <Button busy={check.isPending}
              onClick={() => check.mutateAsync(draft.name)
                .then((result) => result.ok
                  ? toast.ok(`${draft.name} answered in ${result.took_ms ?? "?"}ms.`)
                  : toast.err(result.error ?? "no answer"))
                .catch((error) => toast.err(String(error)))}>Test</Button>
            <Button variant="danger" busy={remove.isPending}
              onClick={() => { if (confirm(`Delete ${draft.name}?`)) {
                remove.mutateAsync(draft.name).catch((error) => toast.err(String(error)));
              } }}>Delete</Button>
            <div className="flex-1" />
            <Badge tone={draft.self_contained ? "ok" : "neutral"}>
              {draft.self_contained ? "self-contained" : "borrows a provider"}
            </Badge>
          </div>
        </div>
      )}
    </div>
  );
}
