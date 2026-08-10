/** Editing a library of named things as one set of pending changes.
 *
 * The editors used to save per item, which made "I changed four agents" into
 * four separate decisions and left no way to back out of any of them. Here every
 * edit goes into a draft, nothing reaches the server until Apply, and Discard
 * puts all of it back — including additions and deletions.
 *
 * The one rule worth stating: server data is adopted only while the draft is
 * clean. A run in progress refetches constantly, and letting that overwrite what
 * someone is halfway through typing is the bug this shape exists to prevent.
 */

import { useCallback, useMemo, useRef, useState } from "react";

export interface DraftLibrary<T> {
  /** Everything as it currently stands: server data with edits applied and
   *  deletions removed. This is what the list renders. */
  items: T[];
  /** The item being edited, or null when the library is empty. */
  selected: T | null;
  selectedName: string;
  select: (name: string) => void;

  edit: (patch: Partial<T>) => void;
  /** Replace the selected item wholesale — for edits a patch cannot express,
   *  like rewriting a loop's nodes. */
  replace: (item: T) => void;
  add: (item: T) => void;
  remove: (name: string) => void;

  dirty: boolean;
  /** How many items are added, changed or deleted — the number on Apply. */
  changeCount: number;
  changed: T[];
  removed: string[];
  isNew: (name: string) => boolean;

  discard: () => void;
  /** Drop the pending edit for one item, so the server's copy shows through
   *  again. For a change that has already landed by another route — restoring
   *  a built-in agent, which the server does in one call. */
  forget: (name: string) => void;
  /** Call after a successful save so the draft stops shadowing the server. */
  settle: () => void;
}

export function useDraftLibrary<T extends object>(
  server: T[] | undefined,
  nameOf: (item: T) => string,
): DraftLibrary<T> {
  const [edits, setEdits] = useState<Record<string, T>>({});
  const [added, setAdded] = useState<T[]>([]);
  const [removed, setRemoved] = useState<string[]>([]);
  const [selectedName, setSelectedName] = useState("");

  const dirty = Object.keys(edits).length > 0 || added.length > 0 || removed.length > 0;

  // Hold the last server snapshot seen while clean. Reading `server` directly
  // would let a refetch mid-edit swap the list under the person editing it.
  const held = useRef<T[]>([]);
  if (!dirty && server) held.current = server;
  const base = dirty ? held.current : (server ?? held.current);

  const items = useMemo(() => {
    const merged = base
      .filter((item) => !removed.includes(nameOf(item)))
      .map((item) => edits[nameOf(item)] ?? item);
    return [...merged, ...added.filter((item) => !removed.includes(nameOf(item)))];
  }, [base, edits, added, removed, nameOf]);

  const selected = useMemo(
    () => items.find((item) => nameOf(item) === selectedName) ?? items[0] ?? null,
    [items, selectedName, nameOf]);

  const currentName = selected ? nameOf(selected) : "";

  /** Identity is the name the item had *before* this edit, never the one it has
   *  after. Keying by the new name broke renaming a freshly added item: the
   *  lookup missed, so it was recorded as an edit to a name that did not exist
   *  and the original stayed in the list — one rename, two items. */
  const write = useCallback((previousName: string, next: T) => {
    if (added.some((item) => nameOf(item) === previousName)) {
      setAdded((list) =>
        list.map((item) => (nameOf(item) === previousName ? next : item)));
    } else {
      setEdits((current) => ({ ...current, [previousName]: next }));
    }
    if (nameOf(next) !== previousName) setSelectedName(nameOf(next));
  }, [added, nameOf]);

  return {
    items,
    selected,
    selectedName: currentName,
    select: setSelectedName,

    edit: (patch) => { if (selected) write(currentName, { ...selected, ...patch }); },
    replace: (item) => write(currentName, item),

    add: (item) => {
      setAdded((list) => [...list, item]);
      setSelectedName(nameOf(item));
    },

    remove: (name) => {
      // A thing added and then deleted before Apply never existed, so it goes
      // rather than becoming a delete the server would reject.
      const wasNew = added.some((item) => nameOf(item) === name);
      setAdded((list) => list.filter((item) => nameOf(item) !== name));
      setEdits(({ [name]: _dropped, ...rest }) => rest);
      if (!wasNew) setRemoved((list) => [...list, name]);
      if (currentName === name) setSelectedName("");
    },

    dirty,
    changeCount: Object.keys(edits).length + added.length + removed.length,
    changed: [...Object.values(edits), ...added],
    removed,
    isNew: (name) => added.some((item) => nameOf(item) === name),

    forget: (name) => setEdits(({ [name]: _dropped, ...rest }) => rest),

    discard: () => { setEdits({}); setAdded([]); setRemoved([]); },
    settle: () => { setEdits({}); setAdded([]); setRemoved([]); },
  };
}
