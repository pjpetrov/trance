/** The shape every named-collection editor shares: a list you add to on the
 *  left, the thing you are editing on the right, and one Apply for the lot.
 *
 *  Agents, loops and command lists are the same kind of object — a library of
 *  named definitions — and the old UI gave each a different layout, no way to
 *  add one except through another screen, and a Save button per item.
 */

import { useState, type ReactNode } from "react";
import { ApiError } from "@/api/client";
import { cn } from "@/lib/cn";
import { Confirm } from "./Confirm";
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
/** Deleting through the library, with usage as a question rather than a wall.
 *
 * A first delete that hits usage answers 409 naming what still points at the
 * thing; the confirm shows exactly that, and "Delete anyway" is the approval —
 * after which steps that still name it fail at run time saying it was deleted.
 * A 404 mid-apply means an earlier confirmed pass already removed it. */
export function useForcedRemoval(
  removeOne: (name: string, force: boolean) => Promise<unknown>,
) {
  const [forcing, setForcing] = useState<{ name: string; detail: string } | null>(null);

  const removeAll = async (names: string[]): Promise<boolean> => {
    for (const name of names) {
      try {
        await removeOne(name, false);
      } catch (error) {
        if (error instanceof ApiError && error.status === 409) {
          setForcing({ name, detail: error.detail });
          return false;
        }
        if (error instanceof ApiError && error.status === 404) continue;
        throw error;
      }
    }
    return true;
  };
  return { forcing, clear: () => setForcing(null), removeAll };
}

export function ForceConfirm(
  { forcing, busy, onClose, onConfirm }:
  { forcing: { name: string; detail: string } | null; busy?: boolean;
    onClose: () => void; onConfirm: (name: string) => void },
) {
  return (
    <Confirm
      open={Boolean(forcing)}
      title={`Delete "${forcing?.name}" anyway?`}
      confirmLabel="Delete anyway"
      danger
      busy={busy}
      onClose={onClose}
      onConfirm={() => forcing && onConfirm(forcing.name)}
    >
      <p>{forcing?.detail}</p>
    </Confirm>
  );
}

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
