/** The shape every named-collection editor shares: a list you add to on the
 *  left, the thing you are editing on the right, and one Apply for the lot.
 *
 *  Agents, loops and command lists are the same kind of object — a library of
 *  named definitions — and the old UI gave each a different layout, no way to
 *  add one except through another screen, and a Save button per item.
 */

import type { ReactNode } from "react";
import { cn } from "@/lib/cn";
import { Badge, Button, Empty } from "./primitives";

export function LibraryList<T>(
  { items, nameOf, selected, onSelect, onAdd, addLabel, meta, isNew, removed }:
  {
    items: T[];
    nameOf: (item: T) => string;
    selected: string;
    onSelect: (name: string) => void;
    onAdd?: () => void;
    addLabel?: string;
    /** Anything worth showing beside the name — a colour, a token count. */
    meta?: (item: T) => ReactNode;
    isNew?: (name: string) => boolean;
    /** Deleted in the draft but not yet applied; shown struck through so the
     *  pending change is visible before it is committed. */
    removed?: string[];
  },
) {
  return (
    <aside className="flex min-h-0 flex-col border-r border-line">
      <div className="min-h-0 flex-1 space-y-0.5 overflow-y-auto p-2">
        {items.map((item) => {
          const name = nameOf(item);
          return (
            <button
              key={name}
              onClick={() => onSelect(name)}
              className={cn(
                "flex w-full items-center gap-2 rounded-[--radius] px-2 py-1.5",
                "text-left text-sm transition-colors hover:bg-panel-2",
                name === selected && "bg-panel-2 ring-1 ring-accent/40",
              )}
            >
              <span className="min-w-0 flex-1 truncate">{name}</span>
              {isNew?.(name) && <Badge tone="ok">new</Badge>}
              {meta?.(item)}
            </button>
          );
        })}

        {(removed ?? []).map((name) => (
          <div key={name}
               className="px-2 py-1.5 text-sm text-muted line-through opacity-60">
            {name}
          </div>
        ))}

        {!items.length && !removed?.length && <Empty title="Nothing here yet." />}
      </div>

      {onAdd && (
        <div className="border-t border-line p-2">
          <Button className="w-full" size="sm" onClick={onAdd}>
            {addLabel ?? "New"}
          </Button>
        </div>
      )}
    </aside>
  );
}

/** The footer every library shares: what is pending, and the two ways out. */
export function LibraryFooter(
  { changeCount, onApply, onApplyDefault, onDiscard, busy, note }:
  {
    changeCount: number;
    onApply: () => void;
    /** Write the same changes into what new sessions start from. Offered
     *  because the usual reason for tuning an agent here is that you want it
     *  that way everywhere, and re-doing the edit in the other scope is the
     *  step people skip. */
    onApplyDefault?: () => void;
    onDiscard: () => void;
    busy?: boolean;
    note?: ReactNode;
  },
) {
  const dirty = changeCount > 0;
  return (
    <>
      <span className="mr-auto text-xs text-muted">
        {note ?? (dirty
          ? `${changeCount} unsaved change${changeCount === 1 ? "" : "s"}`
          : "No changes")}
      </span>
      <Button onClick={onDiscard} disabled={!dirty || busy}>Discard</Button>
      {onApplyDefault && (
        <Button
          onClick={onApplyDefault} disabled={!dirty} busy={busy}
          title="Apply to this session and to what new sessions start from"
        >Apply as default</Button>
      )}
      <Button variant="primary" onClick={onApply} disabled={!dirty} busy={busy}>
        Apply{dirty ? ` (${changeCount})` : ""}
      </Button>
    </>
  );
}
