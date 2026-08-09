/** What the agents produced: the tree, the file, and the preview. */

import { useMemo, useState } from "react";
import { useFile, useFiles, usePreview, useSession } from "@/api/queries";
import { usePreviewMutations, useReviewMutations } from "@/api/mutations";
import { useUi } from "@/store/ui";
import { cn } from "@/lib/cn";
import { Badge, Button, Empty, Panel, PanelHeader, Textarea } from "@/components/ui/primitives";
import { toast } from "@/components/Toaster";
import type { FileNode } from "@/api/types";

export function FilesScreen() {
  const { sessionId, filePath, openFile } = useUi();
  const files = useFiles(sessionId);
  const session = useSession(sessionId);
  const preview = usePreview(sessionId);
  const { start, share } = usePreviewMutations(sessionId ?? "");
  const review = useReviewMutations(sessionId ?? "");
  const [note, setNote] = useState("");

  const tree = useMemo(() => (files.data?.tree as FileNode | undefined) ?? null, [files.data]);

  if (!sessionId) return <Empty title="No session selected." />;

  return (
    <div className="grid h-full min-w-0 grid-cols-[19rem_minmax(0,1fr)] gap-3 p-3">
      <Panel className="flex min-h-0 min-w-0 flex-col">
        <PanelHeader
          title="Files"
          subtitle={files.data
            ? `${files.data.files} files · ${files.data.lines.toLocaleString()} lines`
            : "reading…"}
        />
        <div className="min-h-0 flex-1 overflow-y-auto p-1.5">
          {tree
            ? <Tree node={tree} depth={0} selected={filePath} onOpen={openFile}
                    onPreview={(path) => start.mutateAsync(path)
                      .then((made) => { if (made?.url) window.open(made.local ?? made.url, "_blank"); })
                      .catch((error) => toast.err(String(error)))} />
            : <Empty title="Nothing yet." hint="Files appear as the agents write them." />}
        </div>
      </Panel>

      <div className="grid min-h-0 grid-rows-[1fr_auto] gap-3">
        <FileView sessionId={sessionId} path={filePath} />

        <Panel>
          <PanelHeader
            title="Review"
            subtitle="Comments go back to the team as a test-and-fix step"
            actions={
              <>
                {preview.data?.url && (
                  <Badge tone="accent">
                    <a href={preview.data.local ?? preview.data.url} target="_blank"
                       rel="noreferrer">preview :{preview.data.port}</a>
                  </Badge>
                )}
                <Button size="sm" busy={share.isPending}
                        onClick={() => share.mutateAsync(undefined)
                          .then((result) => result.url
                            ? navigator.clipboard.writeText(result.url).then(
                                () => toast.ok(`Share link copied: ${result.url}`))
                            : toast.info("Sharing stopped."))
                          .catch((error) => toast.err(String(error)))}>
                  share
                </Button>
              </>
            }
          />
          <div className="space-y-2 p-3">
            {(session.data?.review ?? []).map((comment, index) => (
              <div key={index} className="flex items-start gap-2 rounded-[--radius] bg-panel-2 p-2">
                <span className="min-w-0 flex-1 text-xs">
                  {comment.path && (
                    <span className="text-accent">{comment.path}
                      {comment.line ? `:${comment.line}` : ""} — </span>
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
                rows={2} value={note} placeholder={filePath
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

function Tree(
  { node, depth, selected, onOpen, onPreview }:
  { node: FileNode; depth: number; selected: string | null;
    onOpen: (path: string) => void; onPreview: (path: string) => void },
) {
  const [open, setOpen] = useState(depth < 2);
  const children = node.children ?? [];
  const servable = /\.html?$/i.test(node.name);

  if (node.is_dir) {
    return (
      <div>
        {depth > 0 && (
          <button
            onClick={() => setOpen(!open)}
            className="flex w-full items-center gap-1 rounded-[--radius] px-1.5 py-0.5
                       text-left text-xs text-muted hover:bg-panel-2"
            style={{ paddingLeft: depth * 10 }}
          >
            <span className="w-3">{open ? "▾" : "▸"}</span>{node.name}
          </button>
        )}
        {open && children.map((child) => (
          <Tree key={child.path} node={child} depth={depth + 1} selected={selected}
                onOpen={onOpen} onPreview={onPreview} />
        ))}
      </div>
    );
  }

  return (
    <div
      className={cn("group flex items-center gap-1 rounded-[--radius] px-1.5 py-0.5",
                    "cursor-pointer text-xs hover:bg-panel-2",
                    node.path === selected && "bg-panel-2 text-accent")}
      style={{ paddingLeft: depth * 10 }}
      onClick={() => onOpen(node.path)}
    >
      <span className="min-w-0 flex-1 truncate">{node.name}</span>
      {servable && (
        <button
          title="Serve this page"
          className="opacity-0 transition-opacity group-hover:opacity-100"
          onClick={(event) => { event.stopPropagation(); onPreview(node.path); }}
        >▷</button>
      )}
    </div>
  );
}

function FileView({ sessionId, path }: { sessionId: string; path: string | null }) {
  const file = useFile(sessionId, path);

  return (
    <Panel className="flex min-h-0 min-w-0 flex-col">
      <PanelHeader
        title={path ?? "No file open"}
        subtitle={file.data ? `${file.data.lines} lines · ${file.data.bytes} bytes` : undefined}
      />
      <div className="min-h-0 flex-1 overflow-auto">
        {file.data
          ? (
            <pre className="font-code p-3">
              {file.data.content.split("\n").map((line, index) => (
                <div key={index} className="flex">
                  <span className="w-12 shrink-0 select-none pr-3 text-right text-muted/50">
                    {index + 1}
                  </span>
                  <span className="whitespace-pre-wrap break-words">{line || " "}</span>
                </div>
              ))}
            </pre>
          )
          : <Empty title="Pick a file to read it." />}
      </div>
    </Panel>
  );
}
