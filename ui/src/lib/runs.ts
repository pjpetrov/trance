/** Cutting a step's events into runs.
 *
 * A *run* is one press of Start or Rerun: every retry inside it, and for a loop
 * step every block of every agent in it. That is the unit anybody means by
 * "what happened last time", and it is not the same as an attempt — a loop step
 * makes a dozen attempts in one run.
 *
 * The engine emits `step_run_started` at each execution. Sessions that predate
 * that marker have no boundaries at all, so their events come back as a single
 * run rather than as nothing: an empty history reads as a bug, and "this ran
 * before runs were recorded" is the truth.
 */

import type { TranceEvent } from "@/api/types";

export const RUN_MARKER = "step_run_started";

export interface Run {
  /** 1-based, as the engine counts them. 0 means "before runs were recorded". */
  n: number;
  events: TranceEvent[];
  startedAt: string;
  /** SUCCESS / FAILED / PASS / FAIL, whichever the run ended on. */
  outcome: string;
  /** Still going: nothing in it says how it ended. */
  running: boolean;
  label: string;
}

const ENDINGS = new Set(["step_outcome", "step_finished", "step_failed", "verdict"]);

function endingOf(events: TranceEvent[]): string {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]!;
    if (!ENDINGS.has(event.type)) continue;
    const payload = event.payload ?? {};
    const said = payload.outcome ?? payload.exit ?? payload.verdict ?? payload.result;
    if (typeof said === "string" && said) return said;
    if (event.type === "step_failed") return "FAILED";
    if (event.type === "step_finished") return "SUCCESS";
  }
  return "";
}

export function splitIntoRuns(events: TranceEvent[]): Run[] {
  const runs: Run[] = [];
  let current: TranceEvent[] = [];
  let n = 0;
  let startedAt = events[0]?.ts ?? "";

  const close = () => {
    if (!current.length && !runs.length) return;
    if (!current.length) return;
    const outcome = endingOf(current);
    runs.push({
      n, events: current, startedAt, outcome,
      running: !outcome,
      label: n ? `run ${n}` : "before runs were recorded",
    });
  };

  for (const event of events) {
    if (event.type === RUN_MARKER) {
      close();
      current = [event];
      n = Number((event.payload as { run?: number })?.run ?? n + 1);
      startedAt = event.ts;
      continue;
    }
    current.push(event);
  }
  close();

  return runs;
}

/** The run to show when a step is opened: the one still going, else the last. */
export function currentRun(runs: Run[]): Run | null {
  return runs.find((run) => run.running) ?? runs[runs.length - 1] ?? null;
}
