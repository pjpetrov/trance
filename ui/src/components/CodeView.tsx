/** A file, highlighted, with a gutter you can leave a comment on.
 *
 * The line number is the target on purpose: pointing at the line you mean is
 * the whole difference between "the collision check is wrong" and a note the
 * next agent has to go and locate. Lines that already carry a comment are
 * marked, so a review reads back off the file itself.
 */

import { useEffect, useRef, useState } from "react";
import { loadCodeMirror, modeFor, type CodeMirrorEditor } from "@/lib/codemirror";
import { Spinner } from "@/components/ui/primitives";

const GUTTER = "trance-comments";

export interface LineComment {
  /** The server addresses a note by its id, not its position. */
  id: string;
  line: number;
  note: string;
}

export function CodeView(
  { path, content, comments, onPickLine, onRemove, activeLine, editing, onChange }:
  {
    path: string;
    content: string;
    /** Comments on this file, with the line each one is about. */
    comments: LineComment[];
    onPickLine: (line: number) => void;
    onRemove: (noteId: string) => void;
    activeLine: number | null;
    /** Read-only unless someone asked to edit. Reviewing is the common case,
     *  and an accidental keystroke in a file an agent is about to read is a
     *  change nobody meant to make. */
    editing?: boolean;
    onChange?: (text: string) => void;
  },
) {
  const host = useRef<HTMLDivElement>(null);
  const editor = useRef<CodeMirrorEditor | null>(null);
  const [failed, setFailed] = useState("");
  const [ready, setReady] = useState(false);
  const observer = useRef<ResizeObserver | null>(null);

  // Held in a ref so the gutter handler, which is registered once, always sees
  // the current callback rather than the one from first render.
  const pick = useRef(onPickLine);
  pick.current = onPickLine;
  const changed = useRef(onChange);
  changed.current = onChange;
  const drop = useRef(onRemove);
  drop.current = onRemove;
  const widgets = useRef<{ clear(): void }[]>([]);

  useEffect(() => {
    let live = true;
    loadCodeMirror()
      .then((CodeMirror) => {
        if (!live || !host.current || editor.current) return;
        const instance = CodeMirror(host.current, {
          value: content,
          mode: modeFor(path),
          theme: "material-darker",
          lineNumbers: true,
          readOnly: true,                          // until Edit says otherwise
          lineWrapping: true,
          styleActiveLine: true,
          matchBrackets: true,
          gutters: ["CodeMirror-linenumbers", GUTTER],
        });
        instance.on("gutterClick", ((_cm: unknown, line: number) => {
          pick.current(line + 1);                 // CodeMirror counts from zero
        }) as never);
        instance.on("change", (() => {
          changed.current?.(instance.getValue());
        }) as never);
        editor.current = instance;
        setReady(true);

        // CodeMirror measures once, at construction. In a flex column it is
        // built before the layout has settled, so it sizes itself to whatever
        // height it saw — which is how an 809-line file rendered sixteen lines
        // and a lot of empty space. Re-measure whenever the box changes.
        const watch = new ResizeObserver(() => instance.refresh());
        watch.observe(host.current);
        observer.current = watch;
      })
      .catch((error: Error) => { if (live) setFailed(error.message); });
    return () => {
      live = false;
      observer.current?.disconnect();
      widgets.current.forEach((widget) => widget.clear());
      widgets.current = [];
    };
  }, []);                                          // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const instance = editor.current;
    if (!instance) return;
    // Only when it differs: setValue during editing would move the cursor to
    // the top on every keystroke, since each one calls back with the new text.
    if (instance.getValue() !== content) instance.setValue(content);
    instance.setOption("mode", modeFor(path));
    instance.refresh();
  }, [content, path, ready]);

  useEffect(() => {
    editor.current?.setOption("readOnly", !editing);
  }, [editing, ready]);

  // Comments live on the line they are about, not in a list somewhere else.
  // A gutter dot says one exists; the note itself has to be readable where the
  // code is, or a review is two things you have to hold in your head at once.
  useEffect(() => {
    const instance = editor.current;
    if (!instance) return;

    widgets.current.forEach((widget) => widget.clear());
    widgets.current = [];
    instance.clearGutter(GUTTER);

    for (const comment of comments) {
      if (comment.line < 1 || comment.line > instance.lineCount()) continue;

      const dot = document.createElement("span");
      dot.textContent = "●";
      dot.title = comment.note;
      dot.className = "trance-note-dot";
      instance.setGutterMarker(comment.line - 1, GUTTER, dot);

      const widget = document.createElement("div");
      widget.className = "trance-note";
      const text = document.createElement("span");
      text.textContent = comment.note;
      const remove = document.createElement("button");
      remove.textContent = "✕";
      remove.title = "Remove this comment";
      remove.className = "trance-note-remove";
      remove.onclick = (event) => {
        event.preventDefault();
        event.stopPropagation();
        drop.current(comment.id);
      };
      widget.append(text, remove);
      widgets.current.push(
        instance.addLineWidget(comment.line - 1, widget, { coverGutter: false }));
    }
  }, [comments, content, ready]);

  useEffect(() => {
    const instance = editor.current;
    if (!instance || activeLine === null) return;
    instance.scrollIntoView({ line: Math.max(0, activeLine - 1), ch: 0 }, 80);
  }, [activeLine]);

  if (failed) {
    return (
      <div className="p-4 text-xs text-err">
        The editor could not load ({failed}). The file is still readable through
        the API; this is a missing asset rather than a problem with the project.
      </div>
    );
  }

  return (
    <div className="relative h-full">
      {!ready && (
        <div className="absolute inset-0 grid place-items-center">
          <Spinner className="text-muted" />
        </div>
      )}
      <div ref={host} className="cm-host" />
    </div>
  );
}
