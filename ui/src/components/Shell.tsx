/** The frame every screen sits in: session, navigation, run controls.
 *
 * These three were spread across the old markup — the session picker in one
 * corner, the run buttons inside the run screen, the nav as four detached
 * anchors — so which session the buttons applied to was never stated anywhere.
 * Here it is one bar, and it reads left to right: which project, which view,
 * what the run is doing.
 */

import { useConfig, useSession, useSessions } from "@/api/queries";
import { useRunControl, useStartRun } from "@/api/mutations";
import type { SocketState } from "@/hooks/useSessionSocket";
import { useUi, type Screen } from "@/store/ui";
import { cn } from "@/lib/cn";
import { duration } from "@/lib/format";
import { Badge, Button, Dot, Select, type Tone } from "@/components/ui/primitives";
import { toast } from "@/components/Toaster";
import { HomeScreen } from "@/screens/HomeScreen";
import { PlanScreen } from "@/screens/PlanScreen";
import { RunScreen } from "@/screens/RunScreen";
import { FilesScreen } from "@/screens/FilesScreen";
import { Modals } from "@/modals/Modals";
import type { SessionStatus } from "@/api/types";

const SCREENS: { id: Screen; label: string }[] = [
  { id: "home", label: "Brief" },
  { id: "plan", label: "Plan" },
  { id: "run", label: "Run" },
  { id: "files", label: "Files" },
];

/** One place deciding what a status looks like, so the dot in the picker and
 *  the badge in the bar can never disagree. */
export function statusTone(status: SessionStatus | undefined): Tone {
  switch (status) {
    case "running": return "accent";
    case "finished": return "ok";
    case "paused": return "warn";
    case "error":
    case "halted": return "err";
    default: return "neutral";
  }
}

export function Shell({ socket }: { socket: SocketState }) {
  const { sessionId, screen, go } = useUi();
  const sessions = useSessions();
  const session = useSession(sessionId);
  const config = useConfig();

  return (
    <div className="flex h-full flex-col">
      <TopBar socket={socket} />

      {config.data?.stale && (
        <div className="border-b border-warn/30 bg-warn-soft px-4 py-1.5 text-xs text-warn">
          The source on disk is newer than the running server — restart it, or you are
          testing code that is not running.
        </div>
      )}

      <nav className="flex items-center gap-1 border-b border-line bg-panel px-3">
        {SCREENS.map((tab) => (
          <button
            key={tab.id}
            onClick={() => go(tab.id)}
            className={cn(
              "relative px-3 py-2 text-sm transition-colors",
              screen === tab.id ? "text-fg" : "text-muted hover:text-fg",
            )}
          >
            {tab.label}
            {screen === tab.id && (
              <span className="absolute inset-x-2 -bottom-px h-0.5 rounded-full bg-accent" />
            )}
          </button>
        ))}
      </nav>

      <main className="min-h-0 flex-1 overflow-hidden">
        {!sessions.data?.length && screen !== "home"
          ? <HomeScreen />
          : screen === "home" ? <HomeScreen />
          : screen === "plan" ? <PlanScreen />
          : screen === "run" ? <RunScreen />
          : <FilesScreen />}
      </main>

      <Modals />
      {session.data?.error && (
        <div className="border-t border-err/40 bg-err-soft px-4 py-2 text-xs text-err">
          {session.data.error}
        </div>
      )}
    </div>
  );
}

function TopBar({ socket }: { socket: SocketState }) {
  const { sessionId, selectSession, openModal } = useUi();
  const sessions = useSessions();
  const session = useSession(sessionId);
  const start = useStartRun(sessionId ?? "");
  const { pause, resume, stop } = useRunControl(sessionId ?? "");

  const live = session.data;
  const running = live?.status === "running";
  const paused = Boolean(live?.paused);

  const run = (action: { mutateAsync: () => Promise<unknown> }, what: string) => () => {
    action.mutateAsync().catch((error: unknown) => toast.err(`${what} failed — ${error}`));
  };

  return (
    <header className="flex items-center gap-3 border-b border-line bg-panel px-3 py-2">
      <span className="select-none text-sm font-semibold tracking-tight text-accent">
        trance
      </span>

      <Select
        aria-label="Session"
        className="h-8 w-64 shrink-0"
        value={sessionId ?? ""}
        onChange={(event) => selectSession(event.target.value || null)}
      >
        {!sessions.data?.length && <option value="">no sessions yet</option>}
        {sessions.data?.map((item) => (
          <option key={item.id} value={item.id}>
            {item.name} — {item.status}
          </option>
        ))}
      </Select>

      {live && (
        <div className="flex min-w-0 items-center gap-2">
          <Dot tone={statusTone(live.status)} pulse={running} />
          <span className="truncate text-xs text-muted">
            {live.progress.done}/{live.progress.total} steps
            {live.run_seconds > 0 && ` · ${duration(live.run_seconds)}`}
          </span>
          {live.progress.failed > 0 && (
            <Badge tone="err">{live.progress.failed} failed</Badge>
          )}
        </div>
      )}

      <div className="flex-1" />

      {socket !== "open" && (
        <Badge tone={socket === "connecting" ? "warn" : "err"}>
          {socket === "connecting" ? "reconnecting" : "offline"}
        </Badge>
      )}

      {live && (
        <div className="flex items-center gap-1.5">
          {!running && (
            <Button variant="primary" size="sm" busy={start.isPending}
                    onClick={run(start, "Start")}>
              Start
            </Button>
          )}
          {running && !paused && (
            <Button size="sm" busy={pause.isPending} onClick={run(pause, "Pause")}>
              Pause
            </Button>
          )}
          {running && paused && (
            <Button variant="primary" size="sm" busy={resume.isPending}
                    onClick={run(resume, "Resume")}>
              Resume
            </Button>
          )}
          {running && (
            <Button variant="danger" size="sm" busy={stop.isPending}
                    onClick={run(stop, "Stop")}>
              Stop
            </Button>
          )}
        </div>
      )}

      <div className="ml-1 flex items-center gap-1">
        <Button variant="ghost" size="sm" onClick={() => openModal("agents")}>Agents</Button>
        <Button variant="ghost" size="sm" onClick={() => openModal("loops")}>Loops</Button>
        <Button variant="ghost" size="sm" onClick={() => openModal("models")}>Models</Button>
        <Button variant="ghost" size="sm" onClick={() => openModal("commands")}>$_</Button>
        <Button variant="ghost" size="sm" onClick={() => openModal("memory")}
                disabled={!sessionId}>Memory</Button>
        <Button variant="ghost" size="sm" onClick={() => openModal("settings")}>Settings</Button>
      </div>
    </header>
  );
}
