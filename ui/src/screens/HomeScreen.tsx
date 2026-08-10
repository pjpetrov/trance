/** Where a session starts: pick one, or describe a new project to the
 *  orchestrator until it has enough to propose a plan. */

import { useRef, useState } from "react";
import { useSession, useSessions, useWorkspace } from "@/api/queries";
import { useChat, useSessionLifecycle } from "@/api/mutations";
import { useUi } from "@/store/ui";
import { cn } from "@/lib/cn";
import { duration } from "@/lib/format";
import { statusTone } from "@/components/Shell";
import { Confirm } from "@/components/ui/Confirm";
import { Checkbox } from "@/components/ui/primitives";
import { api } from "@/api/client";
import { Badge, Button, Dot, Empty, Field, Input, Panel, PanelHeader, Textarea }
  from "@/components/ui/primitives";
import { toast } from "@/components/Toaster";

export function HomeScreen() {
  const { sessionId, selectSession, go } = useUi();
  const [removing, setRemoving] = useState<{ id: string; name: string;
                                             dir: string } | null>(null);
  const [alsoFiles, setAlsoFiles] = useState(false);
  const sessions = useSessions();
  const session = useSession(sessionId);
  const { create, remove } = useSessionLifecycle();
  const chat = useChat(sessionId ?? "");

  return (
    <div className="grid h-full min-w-0 grid-cols-[16rem_minmax(0,1fr)] gap-3 p-3">
      <Panel className="flex min-h-0 min-w-0 flex-col">
        <PanelHeader title="Sessions" subtitle={`${sessions.data?.length ?? 0} in this workspace`} />
        <div className="min-h-0 flex-1 space-y-1 overflow-y-auto p-2">
          {sessions.data?.map((item) => (
            <div
              key={item.id}
              className={cn(
                "group flex items-center gap-2 rounded-[--radius] px-2 py-1.5",
                "cursor-pointer transition-colors hover:bg-panel-2",
                item.id === sessionId && "bg-panel-2 ring-1 ring-accent/40",
              )}
              onClick={() => selectSession(item.id)}
            >
              <Dot tone={statusTone(item.status)} pulse={item.status === "running"} />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm">{item.name}</span>
                <span className="block truncate text-xs text-muted">{item.project_dir}</span>
              </span>
              <Button
                variant="ghost" size="sm"
                className="opacity-0 transition-opacity group-hover:opacity-100"
                onClick={(event) => {
                  event.stopPropagation();
                  setAlsoFiles(false);
                  setRemoving({ id: item.id, name: item.name, dir: item.project_dir });
                }}
              >✕</Button>
            </div>
          ))}
          {!sessions.data?.length && <Empty title="No sessions yet." hint="Create one below." />}
        </div>
        <NewSession onCreate={(body) =>
          create.mutateAsync(body)
            .then((made) => { selectSession(made.id); go("home"); })
            .catch((error) => toast.err(String(error)))} busy={create.isPending} />
      </Panel>

      <Confirm
        open={Boolean(removing)}
        title={`Delete the session "${removing?.name ?? ""}"?`}
        confirmLabel={alsoFiles ? "Delete the session and its files" : "Delete the session"}
        danger
        busy={remove.isPending}
        onClose={() => setRemoving(null)}
        onConfirm={() => {
          if (!removing) return;
          remove.mutateAsync({ sessionId: removing.id, files: alsoFiles })
            .then((result) => {
              if (removing.id === sessionId) selectSession(null);
              setRemoving(null);
              toast.ok(result.files_deleted
                ? "Session and project deleted."
                : "Session deleted. The files are still there.");
            })
            .catch((error) => toast.err(String(error)));
        }}
      >
        <p>
          Its conversation, plan and run history go. The project at{" "}
          <code className="text-accent">{removing?.dir}</code> stays where it is unless
          you say otherwise.
        </p>
        <Checkbox
          label="Delete the project files too"
          hint="Everything in that directory, permanently. Only allowed for a folder inside your workspace — a path you named yourself is refused."
          checked={alsoFiles}
          onChange={(event) => setAlsoFiles(event.target.checked)}
        />
        {alsoFiles && (
          <p className="text-err">
            This cannot be undone from here. If the project is a git repository, its
            history goes with it.
          </p>
        )}
      </Confirm>

      <Panel className="flex min-h-0 min-w-0 flex-col">
        <PanelHeader
          title={session.data?.name ?? "No session"}
          subtitle={session.data
            ? `${session.data.project_dir} — describe a project, a feature or a bug here; `
              + "the orchestrator turns it into steps"
            : undefined}
          actions={session.data && (
            <>
              <Badge tone={statusTone(session.data.status)}>{session.data.status}</Badge>
              {session.data.run_seconds > 0 && (
                <Badge>{duration(session.data.run_seconds)}</Badge>
              )}
              {session.data.chat.length > 0 && (
                <Button size="sm" onClick={() => go("plan")}>
                  Plan{session.data.flow.steps.length
                    ? ` (${session.data.flow.steps.length})` : ""}
                </Button>
              )}
            </>
          )}
        />

        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
          {session.data?.goal && (
            <div className="rounded-[--radius] border border-line bg-panel-2 p-3">
              <div className="mb-1 text-xs font-medium text-muted">What this project is</div>
              <p className="whitespace-pre-wrap text-sm leading-relaxed">{session.data.goal}</p>
            </div>
          )}

          {session.data?.chat.map((message, index) => (
            <div
              key={index}
              className={cn(
                "max-w-[80ch] rounded-[--radius-lg] px-3 py-2 text-sm leading-relaxed",
                message.role === "user"
                  ? "ml-auto bg-accent-soft text-fg"
                  : "border border-line bg-panel-2",
              )}
            >
              <div className="mb-1 text-[11px] uppercase tracking-wide text-muted">
                {message.role === "user" ? "you" : "orchestrator"}
              </div>
              <p className="whitespace-pre-wrap">{message.content}</p>
              {(message.images ?? []).length > 0 && (
                <div className="mt-2 flex flex-wrap gap-2">
                  {message.images!.map((shot) => (
                    <a key={shot} href={api.shotUrl(sessionId!, shot)}
                       target="_blank" rel="noreferrer">
                      <img
                        src={api.shotUrl(sessionId!, shot)} alt="attached screenshot"
                        className="max-h-56 rounded-[--radius] border border-line"
                      />
                    </a>
                  ))}
                </div>
              )}
            </div>
          ))}

          {!session.data && <Empty title="Pick a session, or make one." />}
          {session.data && !session.data.chat.length && (
            <Empty
              title="Tell the orchestrator what you want."
              hint={"A new project, a feature to add, or a bug to fix — describe it and it "
                + "asks what it needs to know before proposing the work. It writes no code "
                + "itself. The more it asks, the better the plan, so let it."}
            />
          )}
        </div>

        {session.data && (
          <Composer
            busy={chat.isPending}
            onSend={(message, images) => chat.mutateAsync({ message, images })
              .catch((error) => { toast.err(String(error)); throw error; })}
          />
        )}
      </Panel>
    </div>
  );
}

/** A name, and nothing else.
 *
 *  It used to ask for an absolute project directory too, which was the same
 *  path every time with a different last component — the name. The server
 *  derives the folder from the name now; this shows which one, so the answer
 *  is visible without being typed.
 */
function NewSession(
  { onCreate, busy }:
  { onCreate: (body: { name: string }) => void; busy: boolean },
) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const workspace = useWorkspace();
  // The same rule the server applies, so what is shown is what is created.
  const folder = name.trim().toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-").replace(/-{2,}/g, "-")
    .replace(/^[-._]+|[-._]+$/g, "").slice(0, 64).replace(/[-._]+$/, "") || "project";

  if (!open) {
    return (
      <div className="border-t border-line p-2">
        <Button className="w-full" onClick={() => setOpen(true)}>New session</Button>
      </div>
    );
  }

  return (
    <form
      className="space-y-2 border-t border-line p-3"
      onSubmit={(event) => {
        event.preventDefault();
        if (!name.trim()) return;
        onCreate({ name: name.trim() });
        setOpen(false); setName("");
      }}
    >
      <Field
        label="Name"
        hint={workspace.data
          ? `${workspace.data.workspace}/${folder} — created if it is not there yet.`
          : "The folder is made in the workspace, from this name."}
      >
        <Input value={name} autoFocus onChange={(e) => setName(e.target.value)}
               placeholder="pacman" />
      </Field>
      <div className="flex gap-2">
        <Button variant="primary" type="submit" busy={busy} className="flex-1">Create</Button>
        <Button type="button" onClick={() => setOpen(false)}>Cancel</Button>
      </div>
    </form>
  );
}

/** The message box: type, paste a screenshot, drop one, or pick one.
 *
 * A picture of the bug is often the whole report — "the ship does not move,
 * here" — and making someone describe a screenshot in words is work they should
 * not have to do. Paste is the one that gets used, so it works without touching
 * the + at all.
 */
function Composer(
  { busy, onSend }:
  { busy: boolean; onSend: (message: string, images: string[]) => Promise<unknown> },
) {
  const [draft, setDraft] = useState("");
  const [images, setImages] = useState<{ id: string; url: string }[]>([]);
  const [over, setOver] = useState(false);
  const picker = useRef<HTMLInputElement>(null);

  const MAX = 4;

  const take = (files: FileList | File[] | null) => {
    const pictures = [...(files ?? [])].filter((file) => file.type.startsWith("image/"));
    if (!pictures.length) return;
    const room = MAX - images.length;
    if (room <= 0) return toast.info(`Four screenshots is the limit.`);
    pictures.slice(0, room).forEach((file) => {
      const reader = new FileReader();
      reader.onload = () => setImages((held) => [
        ...held, { id: `${file.name}:${Date.now()}:${held.length}`,
                   url: String(reader.result) }]);
      reader.readAsDataURL(file);
    });
    if (pictures.length > room) toast.info("Four screenshots is the limit.");
  };

  const send = () => {
    const message = draft.trim();
    if (!message && !images.length) return;
    const held = { message, images: images.map((image) => image.url) };
    setDraft("");
    setImages([]);
    onSend(held.message, held.images).catch(() => {
      // Put it back rather than losing what was typed.
      setDraft(held.message);
      setImages(images);
    });
  };

  return (
    <form
      className={cn("space-y-2 border-t border-line p-3 transition-colors",
                    over && "bg-accent-soft")}
      onSubmit={(event) => { event.preventDefault(); send(); }}
      onDragOver={(event) => { event.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={(event) => {
        event.preventDefault();
        setOver(false);
        take(event.dataTransfer?.files ?? null);
      }}
    >
      {images.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {images.map((image) => (
            <div key={image.id} className="relative">
              <img src={image.url} alt="attached"
                   className="h-20 rounded-[--radius] border border-line" />
              <button
                type="button"
                title="Remove"
                onClick={() => setImages((held) =>
                  held.filter((other) => other.id !== image.id))}
                className="absolute -right-2 -top-2 grid size-6 place-items-center
                           rounded-full border border-line bg-panel text-muted
                           hover:text-err"
              >✕</button>
            </div>
          ))}
        </div>
      )}

      <div className="flex gap-2">
        <Button
          type="button" variant="ghost" title="Attach a screenshot"
          onClick={() => picker.current?.click()}
        >+</Button>
        <input
          ref={picker} type="file" accept="image/png,image/jpeg" multiple hidden
          onChange={(event) => { take(event.target.files); event.target.value = ""; }}
        />
        <Textarea
          rows={4}
          className="text-[13px]"
          value={draft}
          placeholder="A new project, a feature, or a bug — paste or drop a screenshot; ⌘/Ctrl+Enter to send"
          onChange={(event) => setDraft(event.target.value)}
          onPaste={(event) => {
            const files = [...(event.clipboardData?.items ?? [])]
              .filter((item) => item.kind === "file")
              .map((item) => item.getAsFile())
              .filter((file): file is File => Boolean(file));
            if (files.length) { event.preventDefault(); take(files); }
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
              event.preventDefault();
              send();
            }
          }}
        />
        <Button variant="primary" type="submit" busy={busy}>Send</Button>
      </div>
    </form>
  );
}
