/** Loading the vendored CodeMirror, once, on demand.
 *
 * It is CodeMirror 5 and it is plain scripts that assign a global — not modules
 * — so it cannot be imported. Injecting it here rather than putting script tags
 * in index.html means only the screen that shows code pays for it: 400KB that
 * the plan and run pages have no use for.
 *
 * Vendored rather than fetched from a CDN because trance runs on machines with
 * no internet, and an editor is not worth a network dependency.
 */

const BASE = "/static/vendor";

/** Order matters: the modes register themselves against the core, and
 *  htmlmixed depends on xml, javascript and css already being present. */
const SCRIPTS = [
  "codemirror.js",
  "xml.js", "javascript.js", "css.js", "htmlmixed.js", "python.js", "markdown.js",
  "matchbrackets.js", "active-line.js",
];

const STYLES = ["codemirror.css", "material-darker.css"];

export interface CodeMirrorLib {
  (host: HTMLElement, options: Record<string, unknown>): CodeMirrorEditor;
}

export interface CodeMirrorEditor {
  setValue(text: string): void;
  getValue(): string;
  setOption(name: string, value: unknown): void;
  on(event: string, handler: (...args: never[]) => void): void;
  refresh(): void;
  setGutterMarker(line: number, gutter: string, marker: HTMLElement | null): void;
  addLineWidget(line: number, node: HTMLElement,
                options?: Record<string, unknown>): { clear(): void };
  clearGutter(gutter: string): void;
  addLineClass(line: number, where: string, cls: string): void;
  removeLineClass(line: number, where: string, cls: string): void;
  lineCount(): number;
  scrollIntoView(pos: { line: number; ch: number }, margin?: number): void;
  getWrapperElement(): HTMLElement;
}

let pending: Promise<CodeMirrorLib> | null = null;

function loadScript(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const existing = document.querySelector(`script[src="${src}"]`);
    if (existing) return resolve();
    const tag = document.createElement("script");
    tag.src = src;
    tag.onload = () => resolve();
    tag.onerror = () => reject(new Error(`could not load ${src}`));
    document.head.append(tag);
  });
}

function loadStyle(href: string) {
  if (document.querySelector(`link[href="${href}"]`)) return;
  const tag = document.createElement("link");
  tag.rel = "stylesheet";
  tag.href = href;
  document.head.append(tag);
}

export function loadCodeMirror(): Promise<CodeMirrorLib> {
  if (pending) return pending;
  pending = (async () => {
    STYLES.forEach((name) => loadStyle(`${BASE}/${name}`));
    // Sequentially, not in parallel: a mode that arrives before the core has
    // nothing to register itself against and throws.
    for (const name of SCRIPTS) await loadScript(`${BASE}/${name}`);
    const lib = (window as unknown as { CodeMirror?: CodeMirrorLib }).CodeMirror;
    if (!lib) throw new Error("CodeMirror loaded but defined nothing");
    return lib;
  })();
  return pending;
}

/** Which highlighter to use. Unknown extensions get none, which renders as
 *  plain text rather than as the wrong language's colours. */
export function modeFor(path: string): string {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  if (["js", "jsx", "mjs", "cjs", "json"].includes(ext)) return "javascript";
  if (["ts", "tsx"].includes(ext)) return "text/typescript";
  if (ext === "py") return "python";
  if (ext === "css") return "css";
  if (["html", "htm"].includes(ext)) return "htmlmixed";
  if (["md", "markdown"].includes(ext)) return "markdown";
  if (["xml", "svg"].includes(ext)) return "xml";
  return "";
}
