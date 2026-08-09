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

export function CodeView(
  { path, content, commentedLines, onPickLine, activeLine }:
  {
    path: string;
    content: string;
    /** 1-based line numbers that already have a comment. */
    commentedLines: number[];
    onPickLine: (line: number) => void;
    activeLine: number | null;
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
          // Read-only: this view is for reviewing what the agents wrote. Editing
          // belongs to them, and a half-saved edit racing a running step is a
          // conflict nobody asked for.
          readOnly: true,
          lineWrapping: true,
          styleActiveLine: true,
          matchBrackets: true,
          gutters: ["CodeMirror-linenumbers", GUTTER],
        });
        instance.on("gutterClick", ((_cm: unknown, line: number) => {
          pick.current(line + 1);                 // CodeMirror counts from zero
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
    };
  }, []);                                          // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const instance = editor.current;
    if (!instance) return;
    instance.setValue(content);
    instance.setOption("mode", modeFor(path));
    instance.refresh();
  }, [content, path, ready]);

  // Repaint the markers whenever the comments change. Cheap: a file has a
  // handful of comments, not thousands.
  useEffect(() => {
    const instance = editor.current;
    if (!instance) return;
    instance.clearGutter(GUTTER);
    for (const line of commentedLines) {
      if (line < 1 || line > instance.lineCount()) continue;
      const dot = document.createElement("span");
      dot.textContent = "●";
      dot.title = "has a review comment";
      dot.className = "text-warn text-[10px] leading-none";
      instance.setGutterMarker(line - 1, GUTTER, dot);
    }
  }, [commentedLines, content, ready]);

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
