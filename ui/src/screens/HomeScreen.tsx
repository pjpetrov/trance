/** Where a session starts: pick one, or describe a new project to the
 *  orchestrator until it has enough to propose a plan. */

import { useState } from "react";
import { useSession, useSessions } from "@/api/queries";
import { useChat, useSessionLifecycle } from "@/api/mutations";
import { useUi } from "@/store/ui";
import { cn } from "@/lib/cn";
import { duration } from "@/lib/format";
import { statusTone } from "@/components/Shell";
import { Badge, Button, Dot, Empty, Field, Input, Panel, PanelHeader, Textarea }
  from "@/components/ui/primitives";
import { toast } from "@/components/Toaster";

export function HomeScreen() {
  const { sessionId, selectSession, go } = useUi();
  const sessions = useSessions();
  const session = useSession(sessionId);
  const { create, remove } = useSessionLifecycle();
  const chat = useChat(sessionId ?? "");
  const [draft, setDraft] = useState("");

  return (
    <div className="grid h-full min-w-0 grid-cols-[19rem_minmax(0,1fr)] gap-3 p-3">
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
                  if (!confirm(`Delete the session "${item.name}"? The project files stay.`)) return;
                  remove.mutateAsync(item.id).catch((error) => toast.err(String(error)));
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

      <Panel className="flex min-h-0 min-w-0 flex-col">
        <PanelHeader
          title={session.data?.name ?? "No session"}
          subtitle={session.data?.project_dir}
          actions={session.data && (
            <>
              <Badge tone={statusTone(session.data.status)}>{session.data.status}</Badge>
              {session.data.run_seconds > 0 && (
                <Badge>{duration(session.data.run_seconds)}</Badge>
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
            </div>
          ))}

          {!session.data && <Empty title="Pick a session, or make one." />}
          {session.data && !session.data.chat.length && (
            <Empty
              title="Tell the orchestrator what you want built."
              hint="It asks a question or two, then proposes a plan. It writes no code itself."
            />
          )}
        </div>

        {session.data && (
          <form
            className="flex gap-2 border-t border-line p-3"
            onSubmit={(event) => {
              event.preventDefault();
              const message = draft.trim();
              if (!message) return;
              setDraft("");
              chat.mutateAsync(message).catch((error) => {
                toast.err(String(error));
                setDraft(message);
              });
            }}
          >
            <Textarea
              rows={2}
              value={draft}
              placeholder="Describe what you want built…"
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                  event.currentTarget.form?.requestSubmit();
                }
              }}
            />
            <Button variant="primary" type="submit" busy={chat.isPending}>Send</Button>
          </form>
        )}
      </Panel>
    </div>
  );
}

function NewSession(
  { onCreate, busy }:
  { onCreate: (body: { name: string; project_dir: string }) => void; busy: boolean },
) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [dir, setDir] = useState("");

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
        if (!name.trim() || !dir.trim()) return;
        onCreate({ name: name.trim(), project_dir: dir.trim() });
        setOpen(false); setName(""); setDir("");
      }}
    >
      <Field label="Name">
        <Input value={name} autoFocus onChange={(e) => setName(e.target.value)}
               placeholder="pacman" />
      </Field>
      <Field label="Project directory" hint="Absolute path. Created if it does not exist.">
        <Input value={dir} onChange={(e) => setDir(e.target.value)}
               placeholder="/home/you/projects/pacman" />
      </Field>
      <div className="flex gap-2">
        <Button variant="primary" type="submit" busy={busy} className="flex-1">Create</Button>
        <Button type="button" onClick={() => setOpen(false)}>Cancel</Button>
      </div>
    </form>
  );
}
