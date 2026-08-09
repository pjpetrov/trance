/** The team's shared notebook.
 *
 * Every agent reads it at the start of every step and none of them remembers
 * anything else, so it is the only channel between them — and the only place a
 * decision made in step 2 can reach step 9.
 */

import { useEffect, useState } from "react";
import { useMemory } from "@/api/queries";
import { useMemoryMutations } from "@/api/mutations";
import { useUi } from "@/store/ui";
import { Badge, Button, Empty, Textarea } from "@/components/ui/primitives";
import { toast } from "@/components/Toaster";

export function MemoryPanel() {
  const sessionId = useUi((state) => state.sessionId);
  const memory = useMemory(sessionId);
  const { save, compact } = useMemoryMutations(sessionId ?? "");
  const [draft, setDraft] = useState<string | null>(null);

  useEffect(() => { setDraft(null); }, [sessionId]);

  if (!sessionId) return <Empty title="No session selected." />;
  if (!memory.data) return <Empty title="Loading…" />;

  const text = draft ?? memory.data.raw;
  const dirty = draft !== null && draft !== memory.data.raw;

  return (
    <div className="space-y-3 p-5">
      <div className="flex items-center gap-2 text-xs text-muted">
        <span className="font-code truncate">{memory.data.path}</span>
        <div className="flex-1" />
        <Badge tone={memory.data.oversized ? "warn" : "neutral"}>
          {memory.data.notes.length} / {memory.data.max_notes} notes
        </Badge>
      </div>

      {memory.data.oversized && (
        <p className="text-xs leading-snug text-warn">
          This is long enough that every agent pays for it on every step. Compacting asks a
          model to merge the notes that say the same thing.
        </p>
      )}

      <Textarea
        rows={16}
        className="font-code"
        value={text}
        onChange={(event) => setDraft(event.target.value)}
      />

      <div className="flex items-center gap-2">
        <Button
          variant="primary" disabled={!dirty} busy={save.isPending}
          onClick={() => save.mutateAsync(text)
            .then(() => { setDraft(null); toast.ok("Saved."); })
            .catch((error) => toast.err(String(error)))}
        >Save</Button>
        <Button
          busy={compact.isPending}
          onClick={() => compact.mutateAsync()
            .then((result) => {
              setDraft(null);
              toast.ok(result.compacted
                ? `Compacted ${result.before} notes to ${result.after}.`
                : `Left as it was — ${result.reason}`);
            })
            .catch((error) => toast.err(String(error)))}
        >Compact</Button>
        {dirty && <span className="text-xs text-warn">unsaved changes</span>}
      </div>
    </div>
  );
}
