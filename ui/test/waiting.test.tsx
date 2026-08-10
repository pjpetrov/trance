/** Knowing the model is working.
 *
 * A local 27B can spend two minutes on one generation and emits nothing while
 * it does, so the console went quiet and there was no way to tell working from
 * stuck. The runner announces the call before making it; this is the line that
 * announcement turns into.
 */

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { RunScreen } from "@/screens/RunScreen";
import { useUi } from "@/store/ui";
import { fakeServer, renderWithQuery, stubWebSocket } from "./render";
import { eventsRoute, session, step } from "./fixtures";
import type { TranceEvent } from "@/api/types";

const CONTEXT = {
  tokens: 17_700, window: 64_000, budget: 55_000, reserved: 4_096,
  percent: 32, estimated: true,
};

// Unique per event: React drops children that share a key, so a fixture that
// hands out one id can quietly render fewer lines than the test asked for.
let nth = 0;
const event = (type: string, payload: Record<string, unknown>): TranceEvent => ({
  id: `${type}-${(nth += 1)}`, type, session_id: "s1", step_id: "st1",
  ts: new Date().toISOString(), agent: "frontend", payload,
});

beforeEach(() => {
  stubWebSocket();
  useUi.setState({ sessionId: "s1", screen: "run", openStep: null });
});
afterEach(() => vi.unstubAllGlobals());

const running = (events: TranceEvent[]) => fakeServer({
  "/api/sessions/s1": session({
    status: "running",
    flow: { steps: [step({ status: "running" })], cursor: 0 },
  }),
  "/api/sessions/s1/events": eventsRoute(events, events),
});

describe("the console header", () => {
  it("says which model it is waiting for", async () => {
    running([event("model_waiting", {
      preset: "Qwen3.6-llama.cpp", model: "qwen", round: 1, context: CONTEXT,
    })]);

    renderWithQuery(<RunScreen />);
    // Twice: the header, and the console's newest line. It used to be on a
    // console line per round, and the repeats pushed the work off the screen.
    expect(await screen.findAllByText(/waiting for Qwen3.6-llama.cpp/)).toHaveLength(2);
    // The gauge is up before the answer exists, from an estimate that says so.
    expect(screen.getByText(/^17.7k\/55.0k~$/)).toBeInTheDocument();
    expect(screen.getByText("32%")).toBeInTheDocument();
  });

  it("stops waiting once the answer lands", async () => {
    running([
      event("model_waiting", { preset: "Qwen", context: CONTEXT }),
      event("model_call", {
        preset: "Qwen", round: 1, response_text: "done",
        context: { ...CONTEXT, tokens: 18_000, estimated: false },
      }),
    ]);

    renderWithQuery(<RunScreen />);
    // The gauge stays — it is the last reading, and now a reported one.
    await waitFor(() => expect(screen.getByText("33%")).toBeInTheDocument());
    expect(screen.queryByText(/waiting for/)).not.toBeInTheDocument();
    expect(screen.queryByText(/~$/)).not.toBeInTheDocument();
  });

  it("keeps the last reading when the newest event has none", async () => {
    // A step that ran out of rounds ends on a call that carried no gauge, and
    // the header went blank at exactly the moment the window was fullest.
    running([
      event("model_waiting", { preset: "Qwen", context: CONTEXT }),
      event("model_call", { preset: "Qwen", round: 1, response_text: "out of rounds",
                            finish_reason: "max_rounds" }),
    ]);

    renderWithQuery(<RunScreen />);
    await waitFor(() => expect(screen.getByText("32%")).toBeInTheDocument());
    expect(screen.queryByText(/waiting for/)).not.toBeInTheDocument();
  });

  it("marks the calls that went out with thinking off", async () => {
    running([
      event("model_call", { preset: "Qwen", round: 1, thinking: true,
                            tool_calls: [{ name: "read_file", arguments: {} }] }),
      event("model_call", { preset: "Qwen", round: 2, thinking: false,
                            response_text: "Right, done." }),
      event("model_call", { preset: "Qwen", round: 3, response_text: "no toggle here" }),
    ]);

    renderWithQuery(<RunScreen />);
    // Every call used to read "thinking", including the ones sent without it.
    expect(await screen.findByText("no thinking")).toBeInTheDocument();
    // And a backend whose thinking trance does not set is not called either way.
    expect(screen.getAllByText("thinking")).toHaveLength(2);
  });

  it("keeps only the newest waiting line in the console", async () => {
    // A step doing twenty rounds of reads showed twenty identical lines, so the
    // work scrolled off the screen. One line, for the call that has not come
    // back, is what tells you it is working rather than stuck.
    running([
      event("model_waiting", { preset: "Qwen", round: 1, context: CONTEXT }),
      event("model_call", { preset: "Qwen", round: 1, response_text: "ok" }),
      event("model_waiting", { preset: "Qwen", round: 2, context: CONTEXT }),
    ]);

    renderWithQuery(<RunScreen />);
    // Header plus one console line: the first waiting event is history now.
    await waitFor(() => expect(screen.getAllByText(/waiting for/)).toHaveLength(2));
    // And it counts, so a two-minute generation looks like a two-minute
    // generation rather than a frozen page.
    expect(screen.getByText(/^\d+s$/)).toBeInTheDocument();
  });

  it("does not count up on a run that has stopped", async () => {
    const events = [event("model_waiting", { preset: "Qwen", round: 9, context: CONTEXT })];
    fakeServer({
      "/api/sessions/s1": session({
        status: "halted",
        flow: { steps: [step({ status: "failed" })], cursor: 0 },
      }),
      "/api/sessions/s1/events": eventsRoute(events, events),
    });

    renderWithQuery(<RunScreen />);
    await screen.findByText(/Build the maze renderer/);
    // Nothing is waiting for anything: the run ended on that line hours ago.
    expect(screen.queryByText(/waiting for/)).not.toBeInTheDocument();
  });

  it("does not count on a halted step, however its events ended", async () => {
    // What the screenshot showed: a halted session, the console still saying
    // "still going", and the header counting 3602s — an hour since the engine
    // stopped — because the run's last event happened to be a waiting one.
    useUi.setState({ openStep: "st1" });
    const events = [
      event("step_run_started", { run: 2 }),
      event("model_waiting", { preset: "Qwen", round: 1, context: CONTEXT }),
    ];
    fakeServer({
      "/api/sessions/s1": session({
        status: "halted",
        flow: { steps: [step({ id: "st1", status: "halted" })], cursor: 0 },
      }),
      "/api/sessions/s1/events": eventsRoute(events, events),
    });

    renderWithQuery(<RunScreen />);
    await screen.findByText(/Build the maze renderer/);
    await waitFor(() =>
      expect(screen.queryByText(/waiting for/)).not.toBeInTheDocument());
    expect(screen.queryByText(/still going/)).not.toBeInTheDocument();
  });

  it("shows nothing at all before any model has been called", async () => {
    running([]);
    renderWithQuery(<RunScreen />);
    // The running step opens itself, so this is its console, and it is empty.
    await screen.findByText(/Nothing was recorded for this step/);
    expect(screen.queryByText(/waiting for/)).not.toBeInTheDocument();
    expect(screen.queryByText(/%$/)).not.toBeInTheDocument();
  });
});
