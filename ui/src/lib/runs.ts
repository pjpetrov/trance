/** Cutting a step's events into runs, and a run into agent blocks.
 *
 * A *run* is one press of Start: every retry inside it, and for a loop step
 * every block of every agent in it. A *block* is one agent's turn within that
 * run. Those are the two units anybody talks in — "what happened last time" and
 * "what did the reviewer say" — and the raw event stream has neither.
 */

import type { TranceEvent } from "@/api/types";

export const RUN_MARKER = "step_run_started";

/** Events that begin one agent's turn. `loop_node` for a loop step,
 *  `step_started` for a plain one; a step is only ever one of the two. */
const BLOCK_MARKERS = new Set(["loop_node", "step_started", "fixing"]);

const ENDINGS = new Set(["step_outcome", "step_finished", "step_failed", "verdict"]);

export interface Run {
  /** 1-based, as the engine counts them. 0 when reconstructed from older
   *  events, which carry no run marker. */
  n: number;
  events: TranceEvent[];
  startedAt: string;
  outcome: string;
  running: boolean;
  label: string;
  /** True when the boundary was inferred rather than recorded. */
  inferred: boolean;
}

export interface Block {
  key: string;
  agent: string;
  label: string;
  events: TranceEvent[];
  outcome: string;
  running: boolean;
  /** One line saying what came of it, for when the block is folded. */
  summary: string;
  startedAt: string;
}

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

/** Does this event begin a fresh *execution* rather than a retry within one?
 *
 *  Only used for events recorded before run markers existed. The engine numbers
 *  attempts from 1 within an execution and loop blocks from visit 1, so either
 *  landing back on 1 is a new press of the button. Not perfect — a run that
 *  never got past its first attempt looks the same either way — but it turns
 *  "one run, ever" into the several the user knows they did.
 */
function startsExecution(event: TranceEvent): boolean {
  const payload = (event.payload ?? {}) as { attempt?: number; visit?: number; loop?: number };
  if (event.type === "step_started") return (payload.attempt ?? payload.loop ?? 1) === 1;
  if (event.type === "loop_node") return (payload.visit ?? 1) === 1;
  return false;
}

export function splitIntoRuns(events: TranceEvent[]): Run[] {
  if (!events.length) return [];

  const recorded = events.some((event) => event.type === RUN_MARKER);
  const isBoundary = recorded
    ? (event: TranceEvent) => event.type === RUN_MARKER
    : startsExecution;

  const runs: Run[] = [];
  let current: TranceEvent[] = [];
  let n = 0;
  let startedAt = events[0]!.ts;

  const close = () => {
    if (!current.length) return;
    const outcome = endingOf(current);
    runs.push({
      n, events: current, startedAt, outcome, running: !outcome,
      inferred: !recorded,
      label: n ? `run ${n}` : "earlier run",
    });
  };

  for (const event of events) {
    if (isBoundary(event)) {
      close();
      current = [event];
      startedAt = event.ts;
      n = recorded
        ? Number((event.payload as { run?: number })?.run ?? n + 1)
        : n + 1;
      continue;
    }
    current.push(event);
  }
  close();

  // Anything before the first boundary is a run too — a step that only ever
  // ran under an older trance would otherwise have no history at all.
  if (runs.length && runs[0]!.events[0] && !isBoundary(runs[0]!.events[0])) {
    runs[0]!.label = "before runs were recorded";
    runs[0]!.n = 0;
  }
  return runs;
}

/** The run to show when a step is opened: the one still going, else the last. */
export function currentRun(runs: Run[]): Run | null {
  return runs.find((run) => run.running) ?? runs[runs.length - 1] ?? null;
}

/** One line for a folded block: what the agent said it did, or why it failed. */
function summarize(events: TranceEvent[]): string {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const payload = events[index]!.payload ?? {};
    const said = payload.reason ?? payload.summary ?? payload.message;
    if (typeof said === "string" && said.trim()) return said.trim();
  }
  // Nothing said outright — fall back to the last thing the model wrote, which
  // is usually its own summary of the turn.
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]!;
    if (event.type !== "model_call") continue;
    const text = event.payload?.response_text;
    if (typeof text === "string" && text.trim()) return text.trim().split("\n")[0]!;
  }
  return "";
}

function labelFor(marker: TranceEvent | undefined, agent: string): string {
  if (!marker) return agent || "before the first agent";
  const payload = (marker.payload ?? {}) as {
    loop?: string; visit?: number; of?: number; attempt?: number;
  };
  if (marker.type === "loop_node") {
    return `${agent} · block ${payload.visit ?? "?"}${payload.of ? ` of ${payload.of}` : ""}`;
  }
  if (marker.type === "fixing") return `${agent} · fixing`;
  return `${agent}${payload.attempt && payload.attempt > 1 ? ` · attempt ${payload.attempt}` : ""}`;
}

/** Cut a run into one group per agent turn. */
export function splitIntoBlocks(events: TranceEvent[]): Block[] {
  const blocks: Block[] = [];
  let marker: TranceEvent | undefined;
  let current: TranceEvent[] = [];

  const close = () => {
    if (!current.length) return;
    const agent = marker?.agent ?? current.find((event) => event.agent)?.agent ?? "";
    const outcome = endingOf(current);
    blocks.push({
      key: `${marker?.id ?? current[0]!.id}`,
      agent,
      label: labelFor(marker, agent),
      events: current,
      outcome,
      running: !outcome,
      summary: summarize(current),
      startedAt: (marker ?? current[0]!).ts,
    });
  };

  for (const event of events) {
    if (event.type === RUN_MARKER) continue;      // the run's own header
    if (BLOCK_MARKERS.has(event.type)) {
      close();
      marker = event;
      current = [event];
      continue;
    }
    current.push(event);
  }
  close();
  return blocks;
}

/** What a block actually spent itself on, for the header when it is folded.
 *
 * "What is it doing so much?" was unanswerable from the console: one repair
 * turn made 84 lookups and 3 edits, and the only way to know was to unfold it
 * and count. Measured on one real step: 389 lookups against 15 edits, spread
 * over eight blocks — the shape of the problem is in the tally, not in any one
 * line.
 */
export function tallyOf(events: TranceEvent[]): string {
  const counts = { lookups: 0, edits: 0, commands: 0, browser: 0 };
  for (const event of events) {
    if (event.type !== "tool_call") continue;
    const kind = event.payload?.detail?.kind;
    if (kind === "read" || kind === "graph") counts.lookups += 1;
    else if (kind === "write") counts.edits += 1;
    else if (kind === "command" || kind === "background") counts.commands += 1;
    else if (kind === "page" || kind === "key" || kind === "canvas"
             || kind === "wait" || kind === "screenshot") counts.browser += 1;
  }
  const said = (n: number, one: string, many = `${one}s`) =>
    n ? `${n} ${n === 1 ? one : many}` : "";
  return [said(counts.lookups, "lookup"), said(counts.edits, "edit"),
          said(counts.commands, "command"), said(counts.browser, "browser call")]
    .filter(Boolean).join(" · ");
}

/** A read or a graph hit: kept, but not worth looking at unless asked for. */
export function isLookup(event: TranceEvent): boolean {
  if (event.type !== "tool_call") return false;
  const kind = event.payload?.detail?.kind;
  return kind === "read" || kind === "graph";
}
