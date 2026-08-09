/** Cutting a step's events into runs.
 *
 * A run is one press of Start or Rerun — every retry inside it, and for a loop
 * step every block of every agent in it. Nothing in the stream said where one
 * ended and the next began: step_started fires per attempt and loop_node per
 * block, so both would have cut a loop step into a dozen "runs".
 */

import { describe, expect, it } from "vitest";
import { currentRun, splitIntoBlocks, splitIntoRuns } from "@/lib/runs";
import type { TranceEvent } from "@/api/types";

let seq = 0;
const at = (type: string, payload: Record<string, unknown> = {}): TranceEvent => {
  seq += 1;
  return {
    id: `e${seq}`, type, session_id: "s1", step_id: "st1",
    ts: `2026-08-09T10:${String(seq).padStart(2, "0")}:00Z`, payload,
  };
};

const marker = (run: number) => at("step_run_started", { run });

describe("splitting into runs", () => {
  it("keeps every retry and loop block of one execution together", () => {
    const runs = splitIntoRuns([
      marker(1),
      at("loop_node", { visit: 1 }), at("tool_call"), at("step_started", { attempt: 1 }),
      at("loop_node", { visit: 2 }), at("tool_call"),
      at("step_outcome", { outcome: "FAILED" }),
      marker(2),
      at("loop_node", { visit: 1 }), at("tool_call"),
      at("step_outcome", { outcome: "SUCCESS" }),
    ]);

    expect(runs).toHaveLength(2);
    expect(runs[0]!.n).toBe(1);
    expect(runs[0]!.events).toHaveLength(7);       // the marker plus its six
    expect(runs[0]!.outcome).toBe("FAILED");
    expect(runs[1]!.outcome).toBe("SUCCESS");
    expect(runs.every((run) => !run.running)).toBe(true);
  });

  it("reads a run with no ending as still going", () => {
    const runs = splitIntoRuns([marker(3), at("loop_node"), at("tool_call")]);
    expect(runs[0]!.running).toBe(true);
    expect(runs[0]!.outcome).toBe("");
  });

  it("takes a verdict as the ending when that is how the step ended", () => {
    const runs = splitIntoRuns([marker(1), at("verdict", { verdict: "FAIL" })]);
    expect(runs[0]!.outcome).toBe("FAIL");
  });

  it("keeps events from before the marker existed as one run, not none", () => {
    // Every session recorded before this shipped has no markers at all. An
    // empty history reads as a bug; "this ran before runs were recorded" is
    // the truth and is worth saying.
    const runs = splitIntoRuns([at("tool_call"), at("step_outcome", { outcome: "SUCCESS" })]);
    expect(runs).toHaveLength(1);
    expect(runs[0]!.n).toBe(0);
    expect(runs[0]!.label).toMatch(/before runs were recorded/);
  });

  it("returns nothing for a step that has never run", () => {
    expect(splitIntoRuns([])).toEqual([]);
  });
});

describe("which run to show", () => {
  it("prefers the one still going over the last finished one", () => {
    const runs = splitIntoRuns([
      marker(1), at("step_outcome", { outcome: "SUCCESS" }),
      marker(2), at("loop_node"),
    ]);
    expect(currentRun(runs)!.n).toBe(2);
    expect(currentRun(runs)!.running).toBe(true);
  });

  it("falls back to the most recent when nothing is running", () => {
    const runs = splitIntoRuns([
      marker(1), at("step_outcome", { outcome: "FAILED" }),
      marker(2), at("step_outcome", { outcome: "SUCCESS" }),
    ]);
    expect(currentRun(runs)!.n).toBe(2);
  });

  it("has nothing to show for a step that never ran", () => {
    expect(currentRun([])).toBeNull();
  });
});

describe("reconstructing runs from older events", () => {
  it("splits on the first attempt when no run marker was ever written", () => {
    // Every session recorded before the marker shipped collapsed into a single
    // "run", which is not what someone who pressed the button four times sees.
    // The engine numbers attempts from 1 within an execution, so landing back
    // on 1 is a new press.
    const runs = splitIntoRuns([
      at("step_started", { attempt: 1 }), at("tool_call"),
      at("step_started", { attempt: 2 }), at("tool_call"),
      at("step_outcome", { outcome: "FAILED" }),
      at("step_started", { attempt: 1 }), at("tool_call"),
      at("step_outcome", { outcome: "SUCCESS" }),
    ]);

    expect(runs).toHaveLength(2);
    expect(runs[0]!.outcome).toBe("FAILED");
    expect(runs[1]!.outcome).toBe("SUCCESS");
    expect(runs.every((run) => run.inferred)).toBe(true);
  });

  it("splits a loop step on the first block of each execution", () => {
    const runs = splitIntoRuns([
      at("loop_node", { visit: 1 }), at("loop_node", { visit: 2 }),
      at("step_outcome", { outcome: "FAILED" }),
      at("loop_node", { visit: 1 }),
      at("step_outcome", { outcome: "SUCCESS" }),
    ]);
    expect(runs.map((run) => run.outcome)).toEqual(["FAILED", "SUCCESS"]);
  });

  it("prefers the recorded marker over the guess when both are present", () => {
    const runs = splitIntoRuns([
      marker(1), at("loop_node", { visit: 1 }), at("loop_node", { visit: 1 }),
    ]);
    // Two visit-1 blocks in one execution is normal for a loop that came back
    // round; only the marker says where a run really began.
    expect(runs).toHaveLength(1);
    expect(runs[0]!.inferred).toBe(false);
  });
});

describe("folding a run into agent blocks", () => {
  it("groups each agent's turn and says how it went", () => {
    const blocks = splitIntoBlocks([
      marker(1),
      at("loop_node", { visit: 1, of: 8, loop: "visual-test-and-fix" }),
      at("tool_call"), at("step_outcome", { outcome: "FAILED", reason: "only one ghost" }),
      at("loop_node", { visit: 2, of: 8 }),
      at("tool_call"),
    ]);

    expect(blocks).toHaveLength(2);
    expect(blocks[0]!.outcome).toBe("FAILED");
    expect(blocks[0]!.summary).toBe("only one ghost");
    expect(blocks[0]!.label).toContain("block 1 of 8");
    // The one still going has no ending yet — that is what keeps it open.
    expect(blocks[1]!.running).toBe(true);
  });

  it("falls back to what the model last said when nothing stated an outcome", () => {
    const blocks = splitIntoBlocks([
      at("step_started", { attempt: 1 }),
      at("model_call", { response_text: "Wrote the maze renderer.\nThen some detail." }),
    ]);
    expect(blocks[0]!.summary).toBe("Wrote the maze renderer.");
  });

  it("keeps events that arrive before any agent block", () => {
    const blocks = splitIntoBlocks([marker(1), at("git", { message: "checkpoint" })]);
    expect(blocks).toHaveLength(1);
    expect(blocks[0]!.label).toMatch(/before the first agent/);
  });
});
