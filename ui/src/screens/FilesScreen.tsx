/** What the agents produced: the tree, the file, and the preview.
 *
 * The server sends a FLAT list of files with their sizes, plus a per-extension
 * rollup — there is no tree in the API at all, and the tree below is built here
 * from the paths. Assuming otherwise is what crashed this screen: it read
 * `files.data.lines`, a field the endpoint has never returned.
 */

import { useMemo, useState } from "react";
import { useFile, useFiles, usePreview, useSession } from "@/api/queries";
import { usePreviewMutations, useReviewMutations } from "@/api/mutations";
import { useUi } from "@/store/ui";
import { cn } from "@/lib/cn";
import { Badge, Button, Empty, Input, Panel, PanelHeader, Textarea }
  from "@/components/ui/primitives";
import { CodeView } from "@/components/CodeView";
import { toast } from "@/components/Toaster";
import type { ProjectFile, TreeNode } from "@/api/types";

/** Group a flat path list into folders. */
export function buildTree(files: ProjectFile[]): TreeNode {
  const root: TreeNode = { name: "", path: "", children: [] };

  for (const file of files) {
    const parts = file.path.split("/").filter(Boolean);
    let node = root;
    parts.forEach((part, index) => {
      const last = index === parts.length - 1;
      const path = parts.slice(0, index + 1).join("/");
      let child = node.children.find((held) => held.name === part);
      if (!child) {
        child = { name: part, path, children: [] };
        node.children.push(child);
      }
      if (last) child.file = file;
      node = child;
    });
  }

  // Folders first, then alphabetical — the order a file tree is read in.
  const sort = (node: TreeNode) => {
    node.children.sort((a, b) => {
      const aDir = !a.file, bDir = !b.file;
      if (aDir !== bDir) return aDir ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    node.children.forEach(sort);
  };
  sort(root);
  return root;
}

export function FilesScreen() {
  const { sessionId, filePath, openFile } = useUi();
  const listing = useFiles(sessionId);
  const session = useSession(sessionId);
  const preview = usePreview(sessionId);
  const { start, share } = usePreviewMutations(sessionId ?? "");
  const review = useReviewMutations(sessionId ?? "");
  const [note, setNote] = useState("");

  const tree = useMemo(
    () => buildTree(listing.data?.files ?? []), [listing.data]);

  // The counts come from the per-extension rollup, which is the only place the
  // server reports them.
  const totals = useMemo(() => {
    const rows = listing.data?.totals ?? [];
    return rows.reduce(
      (sum, row) => ({
        files: sum.files + row.files,
        lines: sum.lines + row.lines,
        bytes: sum.bytes + row.bytes,
      }),
      { files: 0, lines: 0, bytes: 0 });
  }, [listing.data]);

  const serving = Boolean(preview.data?.port);

  if (!sessionId) return <Empty title="No session selected." />;

  return (
    <div className="grid h-full min-w-0 grid-cols-[19rem_minmax(0,1fr)] gap-3 p-3">
      <Panel className="flex min-h-0 min-w-0 flex-col">
        <PanelHeader
          title="Files"
          subtitle={listing.isLoading
            ? "reading…"
            : `${totals.files} files · ${totals.lines.toLocaleString()} lines`}
        />
        <div className="min-h-0 flex-1 overflow-y-auto p-1.5">
          {tree.children.length
            ? tree.children.map((node) => (
                <Branch
                  key={node.path} node={node} depth={0} selected={filePath}
                  onOpen={openFile}
                  onPreview={(path) => start.mutateAsync(path)
                    .then((made) => {
                      if (made?.port) window.open(`http://localhost:${made.port}/`, "_blank");
                    })
                    .catch((error) => toast.err(String(error)))}
                />
              ))
            : <Empty title="Nothing yet." hint="Files appear as the agents write them." />}
        </div>
      </Panel>

      <div className="grid min-h-0 min-w-0 grid-rows-[1fr_auto] gap-3">
        <FileView
          sessionId={sessionId} path={filePath}
          comments={session.data?.review ?? []}
          onComment={(line, text) => review.add.mutateAsync({ path: filePath!, line, note: text })}
        />

        <Panel className="min-w-0">
          <PanelHeader
            title="Review"
            subtitle="Comments go back to the team as a test-and-fix step"
            actions={
              <>
                {serving && (
                  <a
                    href={`http://localhost:${preview.data!.port}/`}
                    target="_blank" rel="noreferrer"
                  >
                    <Badge tone="accent">preview :{preview.data!.port}</Badge>
                  </a>
                )}
                {preview.data?.public && (
                  <a href={preview.data.public} target="_blank" rel="noreferrer">
                    <Badge tone="ok">public link</Badge>
                  </a>
                )}
                <Button
                  size="sm" busy={share.isPending} disabled={!serving}
                  title={serving ? "Share the running preview" : "Serve a page first"}
                  onClick={() => share.mutateAsync(undefined)
                    .then((result) => result.url
                      ? navigator.clipboard.writeText(result.url).then(
                          () => toast.ok(`Share link copied: ${result.url}`))
                      : toast.info("Sharing stopped."))
                    .catch((error) => toast.err(String(error)))}
                >share</Button>
              </>
            }
          />
          <div className="space-y-2 p-3">
            {(session.data?.review ?? []).map((comment, index) => (
              <div key={index}
                   className="flex items-start gap-2 rounded-[--radius] bg-panel-2 p-2">
                <span className="min-w-0 flex-1 text-xs">
                  {comment.path && (
                    <span className="text-accent">
                      {comment.path}{comment.line ? `:${comment.line}` : ""} —{" "}
                    </span>
                  )}
                  {comment.note}
                </span>
                <Button variant="ghost" size="sm"
                        onClick={() => review.drop.mutateAsync(index)
                          .catch((error) => toast.err(String(error)))}>✕</Button>
              </div>
            ))}

            <div className="flex gap-2">
              <Textarea
                rows={2} value={note}
                placeholder={filePath
                  ? `a note about ${filePath}…`
                  : "a note about the project as a whole…"}
                onChange={(event) => setNote(event.target.value)}
              />
              <div className="flex flex-col gap-1.5">
                <Button
                  size="sm" busy={review.add.isPending}
                  onClick={() => {
                    if (!note.trim()) return;
                    review.add.mutateAsync({ path: filePath ?? undefined, note: note.trim() })
                      .then(() => setNote(""))
                      .catch((error) => toast.err(String(error)));
                  }}
                >add</Button>
                <Button
                  size="sm" variant="primary" busy={review.finish.isPending}
                  disabled={!session.data?.review.length}
                  onClick={() => review.finish.mutateAsync()
                    .then(() => toast.ok("Sent to the team."))
                    .catch((error) => toast.err(String(error)))}
                >send</Button>
              </div>
            </div>
          </div>
        </Panel>
      </div>
    </div>
  );
}

function Branch(
  { node, depth, selected, onOpen, onPreview }:
  { node: TreeNode; depth: number; selected: string | null;
    onOpen: (path: string) => void; onPreview: (path: string) => void },
) {
  const [open, setOpen] = useState(depth < 1);

  if (!node.file) {
    return (
      <div>
        <button
          onClick={() => setOpen(!open)}
          className="flex w-full items-center gap-1 rounded-[--radius] px-1.5 py-0.5
                     text-left text-xs text-muted hover:bg-panel-2"
          style={{ paddingLeft: 6 + depth * 10 }}
        >
          <span className="w-3">{open ? "▾" : "▸"}</span>
          <span className="truncate">{node.name}</span>
          <span className="ml-auto shrink-0 text-[10px] opacity-60">
            {node.children.length}
          </span>
        </button>
        {open && node.children.map((child) => (
          <Branch key={child.path} node={child} depth={depth + 1} selected={selected}
                  onOpen={onOpen} onPreview={onPreview} />
        ))}
      </div>
    );
  }

  const servable = /\.html?$/i.test(node.name);
  return (
    <div
      className={cn("group flex items-center gap-1 rounded-[--radius] px-1.5 py-0.5",
                    "cursor-pointer text-xs hover:bg-panel-2",
                    node.path === selected && "bg-panel-2 text-accent")}
      style={{ paddingLeft: 6 + depth * 10 }}
      onClick={() => onOpen(node.path)}
    >
      <span className="min-w-0 flex-1 truncate">{node.name}</span>
      <span className="shrink-0 text-[10px] text-muted opacity-0 group-hover:opacity-100">
        {node.file.lines}L
      </span>
      {servable && (
        <button
          title="Serve this page"
          className="shrink-0 opacity-0 transition-opacity group-hover:opacity-100"
          onClick={(event) => { event.stopPropagation(); onPreview(node.path); }}
        >▷</button>
      )}
    </div>
  );
}

function FileView(
  { sessionId, path, comments, onComment }:
  {
    sessionId: string; path: string | null;
    comments: { path?: string; line?: number | null; note: string }[];
    onComment: (line: number, note: string) => Promise<unknown>;
  },
) {
  const file = useFile(sessionId, path);
  const [pending, setPending] = useState<number | null>(null);
  const [note, setNote] = useState("");

  const onThisFile = comments.filter((comment) => comment.path === path);
  const lines = onThisFile
    .map((comment) => comment.line)
    .filter((line): line is number => typeof line === "number");

  return (
    <Panel className="flex min-h-0 min-w-0 flex-col">
      <PanelHeader
        title={path ?? "No file open"}
        subtitle={file.data
          ? `${file.data.lines} lines · ${file.data.bytes.toLocaleString()} bytes`
            + " · click a line number to comment on it"
          : undefined}
        actions={onThisFile.length > 0 && (
          <Badge tone="warn">{onThisFile.length} comment(s)</Badge>
        )}
      />

      <div className="min-h-0 flex-1 overflow-hidden">
        {file.isError && (
          <Empty title="That file could not be opened." hint={String(file.error)} />
        )}
        {file.data && (
          <CodeView
            path={file.data.path}
            content={file.data.content}
            commentedLines={lines}
            activeLine={pending}
            onPickLine={(line) => { setPending(line); setNote(""); }}
          />
        )}
        {!file.data && !file.isError && <Empty title="Pick a file to read it." />}
      </div>

      {pending !== null && (
        <form
          className="flex items-start gap-2 border-t border-line p-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (!note.trim()) return;
            onComment(pending, note.trim())
              .then(() => { setPending(null); setNote(""); })
              .catch((error) => toast.err(String(error)));
          }}
        >
          <Badge tone="accent" className="mt-1.5">line {pending}</Badge>
          <Input
            autoFocus value={note} placeholder={`what is wrong at line ${pending}?`}
            onChange={(event) => setNote(event.target.value)}
            onKeyDown={(event) => { if (event.key === "Escape") setPending(null); }}
          />
          <Button size="md" type="submit">add</Button>
          <Button size="md" variant="ghost" type="button" onClick={() => setPending(null)}>
            cancel
          </Button>
        </form>
      )}
    </Panel>
  );
}
