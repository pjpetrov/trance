/** What the agents produced: the tree, the file, and the review you leave on it.
 *
 * The server sends a FLAT list of files with their sizes, plus a per-extension
 * rollup — there is no tree in the API at all, and the tree below is built here
 * from the paths.
 *
 * Reviewing takes no permanent space. A comment lives on the line it is about,
 * a general one is a button that opens a field, and Finish appears only once
 * there is something to finish. A panel sitting at the bottom of every visit
 * costs a third of the screen to say nothing most of the time.
 */

import { useEffect, useMemo, useState } from "react";
import { useFile, useFiles, useLoops, usePreview, useSession } from "@/api/queries";
import { useFileMutations, usePreviewMutations, useReviewMutations }
  from "@/api/mutations";
import { useUi } from "@/store/ui";
import { cn } from "@/lib/cn";
import { Badge, Button, Empty, Input, Panel, PanelHeader, Select, Textarea }
  from "@/components/ui/primitives";
import { Modal } from "@/components/ui/Modal";
import { Confirm } from "@/components/ui/Confirm";
import { CodeView, type LineComment } from "@/components/CodeView";
import { toast } from "@/components/Toaster";
import type { ProjectFile, ReviewComment, RunPlan, TreeNode } from "@/api/types";

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
  const { sessionId, filePath, openFile, go, setOpenStep } = useUi();
  const listing = useFiles(sessionId);
  const session = useSession(sessionId);
  const preview = usePreview(sessionId);
  const { start, share, stop, plan, run } = usePreviewMutations(sessionId ?? "");
  const review = useReviewMutations(sessionId ?? "");
  const files = useFileMutations(sessionId ?? "");
  const [clearing, setClearing] = useState(false);

  const [general, setGeneral] = useState<string | null>(null);
  // What the orchestrator says starts this project, waiting to be confirmed.
  // Never run without that: it is a build on this machine, not a page.
  const [proposal, setProposal] = useState<RunPlan | null>(null);
  const [finishing, setFinishing] = useState(false);

  const tree = useMemo(() => buildTree(listing.data?.files ?? []), [listing.data]);
  const totals = useMemo(() => {
    const rows = listing.data?.totals ?? [];
    return rows.reduce((sum, row) => ({
      files: sum.files + row.files, lines: sum.lines + row.lines,
    }), { files: 0, lines: 0 });
  }, [listing.data]);

  const comments = session.data?.review ?? [];
  const serving = Boolean(preview.data?.port);
  const open = useFile(sessionId, filePath);

  if (!sessionId) return <Empty title="No session selected." />;

  const remove = (noteId: string) =>
    review.drop.mutateAsync(noteId).catch((error) => toast.err(String(error)));

  return (
    <div className="grid h-full min-w-0 grid-cols-[19rem_minmax(0,1fr)] gap-3 p-3">
      <Panel className="flex min-h-0 min-w-0 flex-col">
        <PanelHeader
          title="Files"
          subtitle={listing.isLoading
            ? "reading…"
            : `${totals.files} files · ${totals.lines.toLocaleString()} lines`}
        />
        {!serving && (
          <div className="flex flex-wrap items-center gap-1.5 border-b border-line
                          px-2 py-1.5">
            {/* The two answers to "how should this run", as buttons rather
                than a question — and clear-all beside them, visibly the
                opposite kind of act. */}
            <Button
              size="sm" busy={start.isPending}
              title="Hand the folder to a plain static server — instant, starts nothing.
A Vite or webpack app will load and then fail on its first import; use Start app for those."
              onClick={() => start.mutateAsync(undefined)
                .then((made) => { if (made?.open) window.open(made.open, "_blank"); })
                .catch((error) => toast.err(String(error)))}
            >serve static</Button>
            <Button
              size="sm" variant="primary" busy={plan.isPending || run.isPending}
              title="Ask the orchestrator how this project starts (it reads the README),
show you the command, and run it after you confirm."
              onClick={() => plan.mutateAsync()
                .then((answer) => {
                  if (!answer.static_instead) return setProposal(answer);
                  toast.ok("The orchestrator says this needs no server — serving the files.");
                  return start.mutateAsync(undefined)
                    .then((made) => { if (made?.open) window.open(made.open, "_blank"); });
                })
                .catch((error) => toast.err(String(error)))}
            >start app</Button>
            <div className="flex-1" />
            <Button
              size="sm" variant="ghost"
              title="Remove every generated file. .trance and the git history stay, so it can be undone."
              onClick={() => setClearing(true)}
            >clear all</Button>
          </div>
        )}
        <div className="min-h-0 flex-1 overflow-y-auto p-1.5">
          {tree.children.length
            ? tree.children.map((node) => (
                <Branch
                  key={node.path} node={node} depth={0} selected={filePath}
                  onOpen={openFile}
                  onPreview={(path) => start.mutateAsync(path)
                    .then((made) => { if (made?.open) window.open(made.open, "_blank"); })
                    .catch((error) => toast.err(String(error)))}
                />
              ))
            : <Empty title="Nothing yet." hint="Files appear as the agents write them." />}
        </div>
      </Panel>

      <Panel className="flex min-h-0 min-w-0 flex-col">
        <PanelHeader
          title={filePath ?? "No file open"}
          subtitle={filePath
            ? [open.data && `${open.data.lines} lines`,
               open.data && `${open.data.bytes.toLocaleString()} bytes`,
               "click a line number to comment on that line"]
              .filter(Boolean).join(" · ")
            : "pick a file, or leave a comment about the project as a whole"}
          actions={
            <>
              {serving && (
                <a href={`http://localhost:${preview.data!.port}/`}
                   target="_blank" rel="noreferrer">
                  <Badge tone="accent">serving :{preview.data!.port}</Badge>
                </a>
              )}
              {preview.data?.public && (
                <a href={preview.data.public} target="_blank" rel="noreferrer">
                  <Badge tone="ok">public link</Badge>
                </a>
              )}
              {serving && (
                <>
                  <Button
                    size="sm" busy={share.isPending}
                    title="Make the preview reachable from outside this machine"
                    onClick={() => share.mutateAsync(undefined)
                      .then((result) => result.url
                        ? navigator.clipboard.writeText(result.url).then(
                            () => toast.ok(`Share link copied: ${result.url}`))
                        : toast.info("Sharing stopped."))
                      .catch((error) => toast.err(String(error)))}
                  >share</Button>
                  <Button
                    size="sm" variant="danger" busy={stop.isPending}
                    title="Stop serving these files"
                    onClick={() => stop.mutateAsync()
                      .then(() => toast.ok("Stopped serving."))
                      .catch((error) => toast.err(String(error)))}
                  >stop serving</Button>
                </>
              )}
              <Button
                size="sm" variant={general === null ? "default" : "primary"}
                onClick={() => setGeneral(general === null ? "" : null)}
              >General comment</Button>
              {comments.length > 0 && (
                <Button size="sm" variant="primary" onClick={() => setFinishing(true)}>
                  Review finished ({comments.length})
                </Button>
              )}
            </>
          }
        />

        {general !== null && (
          <form
            className="flex items-start gap-2 border-b border-line p-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (!general.trim()) return;
              review.add.mutateAsync({ note: general.trim() })
                .then(() => setGeneral(null))
                .catch((error) => toast.err(String(error)));
            }}
          >
            <Textarea
              rows={2} autoFocus value={general}
              placeholder="about the project as a whole — what is wrong with the result?"
              onChange={(event) => setGeneral(event.target.value)}
              onKeyDown={(event) => { if (event.key === "Escape") setGeneral(null); }}
            />
            <Button type="submit" busy={review.add.isPending}>add</Button>
            <Button type="button" variant="ghost" onClick={() => setGeneral(null)}>
              cancel
            </Button>
          </form>
        )}

        {/* Comments with no file are about the result rather than any line, so
            they have nowhere in the code to live. */}
        {comments.some((comment) => !comment.path) && (
          <div className="space-y-1 border-b border-line p-2">
            {comments.map((comment) => (!comment.path && (
              <div key={comment.id}
                   className="flex items-start gap-2 rounded-[--radius] bg-warn-soft
                              border-l-2 border-warn px-2 py-1 text-xs">
                <span className="min-w-0 flex-1">{comment.note}</span>
                <button
                  onClick={() => remove(comment.id)}
                  title="Remove this comment"
                  className="-my-0.5 grid size-6 shrink-0 place-items-center rounded-[--radius-sm]
                             text-[15px] leading-none text-muted transition-colors
                             hover:bg-err-soft hover:text-err"
                >✕</button>
              </div>
            )))}
          </div>
        )}

        <FileEditor
          sessionId={sessionId} path={filePath}
          comments={comments}
          onComment={(line, note) =>
            review.add.mutateAsync({ path: filePath!, line, note })}
          onRemove={remove}
          onDeleted={() => openFile(null)}
        />
      </Panel>

      <FinishReview
        open={finishing}
        count={comments.length}
        busy={review.finish.isPending}
        onClose={() => setFinishing(false)}
        onFinish={(loop) => review.finish.mutateAsync(loop)
          .then((result) => {
            setFinishing(false);
            // Straight to where it runs: the point of finishing is to watch it
            // happen, and hunting for the new step on another screen is a step
            // the user should not have to take.
            go("run");
            if (result?.step_id) setOpenStep(result.step_id);
            toast.ok("Sent to the team — it runs next.");
          })
          .catch((error) => toast.err(String(error)))}
      />

      <Confirm
        open={clearing}
        title="Clear all generated files?"
        confirmLabel="Clear them"
        danger
        busy={files.clear.isPending}
        onClose={() => setClearing(false)}
        onConfirm={() => {
          setClearing(false);
          files.clear.mutateAsync()
            .then((out) => toast.ok(
              `Removed ${out.removed} file(s).`
              + (out.committed ? " The wipe is a commit — revert it to undo." : "")))
            .catch((error) => toast.err(String(error)));
        }}
      >
        <p>
          Everything the run built in this project is removed. Two things stay:
          <code> .trance/</code> (trance's own state — the plan, the memory, the
          screenshots) and <code>.git/</code> — the wipe is committed, so this is
          an undoable act, not a shredder.
        </p>
      </Confirm>

      <Confirm
        open={Boolean(proposal)}
        title="Run this?"
        confirmLabel="Run it"
        busy={run.isPending}
        onClose={() => setProposal(null)}
        onConfirm={() => {
          const answer = proposal!;
          setProposal(null);
          run.mutateAsync({ command: answer.command, dir: answer.dir })
            .then((made) => {
              if (made?.open) window.open(made.open, "_blank");
              if (made?.hint) toast.ok(made.hint);
            })
            .catch((error) => toast.err(String(error)));
        }}
      >
        <p>This runs on the machine trance is running on, and keeps running until you
           press stop.</p>
        <pre className="font-code overflow-x-auto rounded-[--radius] bg-bg/60 p-2 text-xs">
          {proposal?.dir ? `cd ${proposal.dir} && ` : ""}{proposal?.command}
        </pre>
        <p className="text-muted">
          {proposal?.why}
          {proposal && !proposal.read_readme
            && " (There is no README, so this is read off the project's files.)"}
        </p>
      </Confirm>
    </div>
  );
}

function FinishReview(
  { open, count, busy, onClose, onFinish }:
  {
    open: boolean; count: number; busy: boolean;
    onClose: () => void; onFinish: (loop: string) => void;
  },
) {
  const sessionId = useUi((state) => state.sessionId);
  const loops = useLoops(sessionId);
  const [loop, setLoop] = useState("");

  return (
    <Modal
      open={open} onClose={onClose}
      title={`Send ${count} comment${count === 1 ? "" : "s"} to the team`}
      subtitle="They become a step that runs next — before anything still pending, because the comments name lines in the code as it is now."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" busy={busy} onClick={() => onFinish(loop)}>
            Send it
          </Button>
        </>
      }
    >
      <div className="space-y-2 p-5">
        <label className="block text-xs font-medium text-muted">How should it be answered?</label>
        <Select value={loop} onChange={(event) => setLoop(event.target.value)}>
          <option value="">let trance choose</option>
          {loops.data?.map((option) => (
            <option key={option.name} value={option.name}>{option.name}</option>
          ))}
        </Select>
        <p className="text-xs leading-relaxed text-muted">
          A plain fix loop makes the change and stops. A test-and-fix loop runs the
          tests afterwards and keeps going until they pass, which is worth it when the
          comments are about behaviour rather than wording. Left to trance, it picks
          based on what the project already has.
        </p>
      </div>
    </Modal>
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
      <span className="shrink-0 tabular-nums text-[10px] text-muted">
        {node.file.lines}
      </span>
      {servable && (
        <button
          title="Serve this page"
          className="grid size-6 shrink-0 place-items-center rounded-[--radius-sm]
                     text-sm leading-none text-muted transition-colors
                     hover:bg-accent-soft hover:text-accent"
          onClick={(event) => { event.stopPropagation(); onPreview(node.path); }}
        >▶</button>
      )}
    </div>
  );
}

function FileEditor(
  { sessionId, path, comments, onComment, onRemove, onDeleted }:
  {
    sessionId: string; path: string | null;
    comments: ReviewComment[];
    onComment: (line: number, note: string) => Promise<unknown>;
    onRemove: (noteId: string) => void;
    onDeleted: () => void;
  },
) {
  const file = useFile(sessionId, path);
  const session = useSession(sessionId);
  const { write, remove } = useFileMutations(sessionId);

  const [pending, setPending] = useState<number | null>(null);
  const [note, setNote] = useState("");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<string | null>(null);
  const [asking, setAsking] = useState<"discard" | "delete" | null>(null);

  // A new file is a new edit. Carrying a draft across files would save one
  // file's contents over another's.
  useEffect(() => {
    setEditing(false); setDraft(null); setPending(null); setAsking(null);
  }, [path]);

  const running = session.data?.status === "running";
  const text = draft ?? file.data?.content ?? "";
  const dirty = draft !== null && draft !== file.data?.content;

  const onThisFile: LineComment[] = comments
    .filter((comment) => comment.path === path && comment.line > 0)
    .map((comment) => ({ id: comment.id, line: comment.line, note: comment.note }));

  const save = () => {
    if (!path || draft === null) return;
    write.mutateAsync({ path, content: draft })
      .then(() => { setDraft(null); setEditing(false); toast.ok(`Saved ${path}.`); })
      .catch((error) => toast.err(String(error)));
  };

  return (
    <>
      {path && file.data && (
        <div className="flex items-center gap-2 border-b border-line px-2 py-1.5">
          {editing ? (
            <>
              <Button size="sm" variant="primary" busy={write.isPending} disabled={!dirty}
                      onClick={save}>Save</Button>
              <Button
                size="sm"
                onClick={() => {
                  // Nothing to lose, nothing to ask.
                  if (!dirty) { setDraft(null); setEditing(false); return; }
                  setAsking("discard");
                }}
              >Cancel</Button>
              {dirty && <span className="text-xs text-warn">unsaved changes</span>}
              {running && (
                <span className="text-xs text-warn">
                  a step is running — an agent may overwrite this
                </span>
              )}
            </>
          ) : (
            <Button size="sm" onClick={() => { setDraft(file.data!.content); setEditing(true); }}>
              Edit
            </Button>
          )}

          <div className="flex-1" />

          <Button
            size="sm" variant="danger" busy={remove.isPending}
            onClick={() => setAsking("delete")}
          >Delete file</Button>
        </div>
      )}

      <Confirm
        open={asking === "discard"}
        title="Discard your changes?"
        confirmLabel="Discard them"
        danger
        onClose={() => setAsking(null)}
        onConfirm={() => { setDraft(null); setEditing(false); setAsking(null); }}
      >
        <p>
          The edits you have made to <b>{path}</b> have not been saved, and closing the
          editor loses them.
        </p>
      </Confirm>

      <Confirm
        open={asking === "delete"}
        title={`Delete ${path}?`}
        confirmLabel="Delete it"
        danger
        busy={remove.isPending}
        onClose={() => setAsking(null)}
        onConfirm={() => {
          if (!path) return;
          remove.mutateAsync(path)
            .then(() => { setAsking(null); onDeleted(); toast.ok(`Deleted ${path}.`); })
            .catch((error) => toast.err(String(error)));
        }}
      >
        <p>It is removed from disk and the deletion is committed, so it is recoverable
          from git — but not from here.</p>
        {running && (
          <p className="text-warn">
            A step is running right now. If an agent is working on this file, deleting it
            underneath will fail that step rather than stopping it.
          </p>
        )}
      </Confirm>

      <div className="min-h-0 flex-1 overflow-hidden">
        {file.isError && (
          <Empty title="That file could not be opened." hint={String(file.error)} />
        )}
        {file.data && (
          <CodeView
            path={file.data.path}
            content={text}
            comments={onThisFile}
            activeLine={pending}
            editing={editing}
            onChange={setDraft}
            onPickLine={(line) => { setPending(line); setNote(""); }}
            onRemove={onRemove}
          />
        )}
        {!file.data && !file.isError && <Empty title="Pick a file to read it." />}
      </div>

      {pending !== null && (
        <form
          className="flex items-center gap-2 border-t border-line p-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (!note.trim()) return;
            onComment(pending, note.trim())
              .then(() => { setPending(null); setNote(""); })
              .catch((error) => toast.err(String(error)));
          }}
        >
          <Badge tone="accent">line {pending}</Badge>
          <Input
            autoFocus value={note} placeholder={`what is wrong at line ${pending}?`}
            onChange={(event) => setNote(event.target.value)}
            onKeyDown={(event) => { if (event.key === "Escape") setPending(null); }}
          />
          <Button type="submit">add</Button>
          <Button variant="ghost" type="button" onClick={() => setPending(null)}>
            cancel
          </Button>
        </form>
      )}
    </>
  );
}
