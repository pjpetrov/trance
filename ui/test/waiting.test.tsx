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
import { session, step } from "./fixtures";
import type { TranceEvent } from "@/api/types";

const CONTEXT = {
  tokens: 17_700, window: 64_000, budget: 55_000, reserved: 4_096,
  percent: 32, estimated: true,
};

const event = (type: string, payload: Record<string, unknown>): TranceEvent => ({
  id: `${type}-1`, type, session_id: "s1", step_id: "st1",
  ts: new Date().toISOString(), agent: "frontend", payload,
});

beforeEach(() => {
  stubWebSocket();
  useUi.setState({ sessionId: "s1", screen: "run", openStep: null });
});
afterEach(() => vi.unstubAllGlobals());

const running = (events: TranceEvent[]) => fakeServer({
  "/api/sessions/s1": session({
    flow: { steps: [step({ status: "running" })], cursor: 0 },
  }),
  "/api/sessions/s1/events": { events, total: events.length, shown: events.length },
});

describe("the console header", () => {
  it("says which model it is waiting for", async () => {
    running([event("model_waiting", {
      preset: "Qwen3.6-llama.cpp", model: "qwen", round: 1, context: CONTEXT,
    })]);

    renderWithQuery(<RunScreen />);
    expect(await screen.findByText(/waiting for Qwen3.6-llama.cpp/)).toBeInTheDocument();
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

  it("shows nothing at all before any model has been called", async () => {
    running([]);
    renderWithQuery(<RunScreen />);
    await screen.findByText(/Start the run, or open a step/);
    expect(screen.queryByText(/waiting for/)).not.toBeInTheDocument();
    expect(screen.queryByText(/%$/)).not.toBeInTheDocument();
  });
});
