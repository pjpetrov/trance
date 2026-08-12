/** The frame every screen sits in: session, navigation, run controls.
 *
 * These three were spread across the old markup — the session picker in one
 * corner, the run buttons inside the run screen, the nav as four detached
 * anchors — so which session the buttons applied to was never stated anywhere.
 * Here it is one bar, and it reads left to right: which project, which view,
 * what the run is doing.
 */

import { useEffect, useState } from "react";
import { useConfig, useSession, useSessions } from "@/api/queries";
import type { SocketState } from "@/hooks/useSessionSocket";
import { useUi, type Screen } from "@/store/ui";
import { cn } from "@/lib/cn";
import { duration } from "@/lib/format";
import { Badge, Button, Dot, type Tone } from "@/components/ui/primitives";
import { HomeScreen } from "@/screens/HomeScreen";
import { PlanScreen } from "@/screens/PlanScreen";
import { RunScreen } from "@/screens/RunScreen";
import { FilesScreen } from "@/screens/FilesScreen";
import { ReviewsScreen } from "@/screens/ReviewsScreen";
import { CommitsScreen } from "@/screens/CommitsScreen";
import { StatsScreen } from "@/screens/StatsScreen";
import { ApprovalPrompt } from "@/components/ApprovalPrompt";
import { Modals } from "@/modals/Modals";
import type { SessionStatus, StepStatus } from "@/api/types";

const SCREENS: { id: Screen; label: string }[] = [
  { id: "home", label: "Chat" },
  { id: "plan", label: "Plan" },
  { id: "run", label: "Run" },
  // Named for what it shows. "Iterations" would collide with a loop's visits,
  // which is a different thing entirely and already on the run screen.
  { id: "commits", label: "History" },
  { id: "files", label: "Files" },
  { id: "reviews", label: "Reviews" },
  { id: "stats", label: "Statistics" },
];

/** One place deciding what a status looks like, so the dot in the picker and
 *  the badge in the bar can never disagree. */
/** A step's status has its own set of words, and reading one map against the
 *  other silently gives every pending step the colour of an error. */
export function stepTone(status: StepStatus | undefined): Tone {
  switch (status) {
    case "running":
    case "verifying": return "accent";
    case "done": return "ok";
    case "failed":
    case "halted": return "err";
    // Not an error: the work is there and a verifier could not say either way.
    // It wants a person, which is what amber means everywhere else here.
    case "blocked":
    case "skipped": return "warn";
    default: return "neutral";
  }
}

/** The word for a status the dot alone cannot carry. Empty when the dot says
 *  it: a green dot is "done" and a pulsing one is "running". */
export function stepWord(status: StepStatus | undefined): string {
  switch (status) {
    case "verifying": return "verifying";
    case "blocked": return "unverified";
    case "skipped": return "skipped";
    case "halted": return "halted";
    default: return "";
  }
}

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

/** The session's working time, ticking while it runs.
 *
 *  It used to redraw only when a snapshot happened to arrive, so it sat
 *  frozen for minutes — which reads as gone, and was reported as gone.
 *  Between snapshots the elapsed time is interpolated locally; each fresh
 *  run_seconds re-anchors it, so drift never outlives one update. */
function WorkClock({ seconds, running }: { seconds: number; running: boolean }) {
  const [anchoredAt, setAnchoredAt] = useState(() => Date.now());
  const [, redraw] = useState(0);

  useEffect(() => { setAnchoredAt(Date.now()); }, [seconds]);
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => redraw((n) => n + 1), 1000);
    return () => clearInterval(timer);
  }, [running]);

  const shown = running ? seconds + (Date.now() - anchoredAt) / 1000 : seconds;
  if (shown <= 0) return null;
  return <> · {duration(shown)}</>;
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
          : screen === "reviews" ? <ReviewsScreen />
          : screen === "commits" ? <CommitsScreen />
          : screen === "stats" ? <StatsScreen />
          : <FilesScreen />}
      </main>

      <Modals />
      <ApprovalPrompt />
      {session.data?.error && (
        <div className="border-t border-err/40 bg-err-soft px-4 py-2 text-xs text-err">
          {session.data.error}
        </div>
      )}
    </div>
  );
}

function TopBar({ socket }: { socket: SocketState }) {
  const { sessionId, openModal, go } = useUi();
  const session = useSession(sessionId);

  const live = session.data;

  return (
    <header className="flex items-center gap-3 border-b border-line bg-panel px-3 py-2">
      <span className="select-none whitespace-nowrap text-sm font-semibold
                       tracking-tight text-accent">
        Trance Harness
      </span>

      {/* The session is chosen on the Chat page, where the list shows what each
          one is and where it got to. A dropdown here said only its name. */}
      {live && (
        <button
          onClick={() => go("home")}
          className="min-w-0 truncate text-sm hover:text-accent"
          title="Sessions are chosen on the Chat page"
        >{live.name}</button>
      )}

      {live && (
        <div className="flex min-w-0 items-center gap-2">
          <Dot tone={statusTone(live.status)} pulse={live.status === "running"} />
          <span className="truncate text-xs text-muted">
            {live.progress.done}/{live.progress.total} steps
            <WorkClock seconds={live.run_seconds} running={live.status === "running"} />
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

      <div className="ml-1 flex items-center gap-1">
        <Button variant="ghost" size="sm" onClick={() => openModal("agents")}>Agents</Button>
        <Button variant="ghost" size="sm" onClick={() => openModal("loops")}>Loops</Button>
        <Button variant="ghost" size="sm" onClick={() => openModal("models")}>Models</Button>
        <Button variant="ghost" size="sm" onClick={() => openModal("commands")}>$_</Button>
        <Button variant="ghost" size="sm" onClick={() => openModal("settings")}>Settings</Button>
      </div>
    </header>
  );
}
