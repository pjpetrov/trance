/** The interface follows the harness.
 *
 * Three places left you behind. The orchestrator turned a conversation into a
 * plan and you stayed on the chat with no sign it had happened; a run started
 * and you stayed on the plan; a step finished and the console kept showing the
 * finished one while the next agent worked in silence. In each case the harness
 * had moved on and the interface had not.
 *
 * It watches the session snapshot rather than hooking each button, because the
 * run can start from the plan screen, the run screen, another tab, or nothing
 * at all — and a rule per button is a rule that will be missed by the next way
 * of starting one.
 *
 * Following a step stops the moment you open a different one, and starts again
 * if you come back to the one that is running. Anything else either yanks the
 * page away while you are reading, or never follows again after one click.
 */

import { useEffect, useRef } from "react";
import { useSession } from "@/api/queries";
import { useUi } from "@/store/ui";

interface Seen {
  session: string;
  steps: number;
  running: boolean;
  step: string | null;
}

export function useFollowHarness(sessionId: string | null) {
  const session = useSession(sessionId);
  const live = session.data;
  const seen = useRef<Seen | null>(null);

  useEffect(() => {
    if (!live) return;
    const now: Seen = {
      session: live.id,
      steps: live.flow.steps.length,
      running: live.status === "running",
      step: live.flow.steps.find((step) => step.status === "running")?.id ?? null,
    };
    const before = seen.current;
    seen.current = now;

    // Read the store rather than subscribing to it: this reacts to the harness,
    // and re-running whenever the user opens a modal would make it fight them.
    const { screen, go, openStep, setOpenStep, follow } = useUi.getState();

    // First sight of a session is not a change. Landing on one that already has
    // a plan, or is already running, must not move you off the screen you asked
    // for — only what happens *next* is worth changing the screen for.
    if (before && before.session === now.session) {
      if (now.steps > 0 && before.steps === 0 && screen === "home") go("plan");
      if (now.running && !before.running) go("run");
    }

    // Selecting a step is not moving you, though: it only decides what the run
    // screen shows when you get there. So it happens on first sight too, or
    // opening a session that is mid-run shows a console scoped to nothing while
    // an agent works. Following it onwards is the console's switch: someone who
    // has paused it is reading something, and moving the step out from under
    // them is the same interruption as scrolling.
    const first = !before || before.session !== now.session;
    if (now.step && now.step !== (before?.step ?? null) && (follow || first)
        && (openStep === null || openStep === before?.step)) {
      setOpenStep(now.step);
    }
  }, [live]);
}
