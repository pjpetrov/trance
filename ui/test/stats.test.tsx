/** What the models have been asked to do.
 *
 * The numbers already existed — every model call reports its usage on the bus
 * and the ledger has counted them all along — but the only place any of it
 * surfaced was a badge on the models modal. "Which model is eating the
 * tokens, and is it input or output" had no answer on screen.
 */

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { screen } from "@testing-library/react";
import { StatsScreen } from "@/screens/StatsScreen";
import { useUi } from "@/store/ui";
import { fakeServer, renderWithQuery, stubWebSocket } from "./render";
import { session, step } from "./fixtures";

const THIS_RUN = {
  models: [
    { model: "Qwen3.6-llama.cpp", calls: 1924,
      input_tokens: 43_460_948, output_tokens: 700_057, total: 44_161_005 },
    { model: "claude-code", calls: 4,
      input_tokens: 6_724_498, output_tokens: 96_050, total: 6_820_548 },
  ],
  total: 50_981_553,
  calls: 1928,
};

const ALL_TIME = {
  models: [
    { model: "Qwen3.6-llama.cpp", calls: 4661,
      input_tokens: 96_326_186, output_tokens: 2_929_993, total: 99_256_179 },
    { model: "a-model-since-deleted", calls: 3,
      input_tokens: 900, output_tokens: 100, total: 1000 },
  ],
  total: 99_257_179,
  calls: 4664,
};

const serve = (over: Record<string, unknown> = {}) => fakeServer({
  "/api/sessions/s1": session({
    run_seconds: 3725,
    flow: {
      steps: [step({ id: "a", status: "done" }), step({ id: "b", status: "done" }),
              step({ id: "c", status: "failed" }), step({ id: "d", status: "pending" })],
      cursor: 0,
    },
  }),
  "/api/sessions/s1/usage": THIS_RUN,
  "/api/usage": ALL_TIME,
  ...over,
});

beforeEach(() => {
  stubWebSocket();
  useUi.setState({ sessionId: "s1", screen: "stats" });
});
afterEach(() => vi.unstubAllGlobals());

describe("the statistics page", () => {
  it("splits each model's tokens into in and out", async () => {
    serve();
    renderWithQuery(<StatsScreen />);

    // It appears in both tables — this session and all time.
    expect(await screen.findAllByText("Qwen3.6-llama.cpp")).toHaveLength(2);
    // 43.4M in against 700K out — the point of showing them apart.
    expect(screen.getByText("43.5M")).toBeInTheDocument();
    expect(screen.getByText("700k")).toBeInTheDocument();
  });

  it("shows input per call, which is the context being re-sent", async () => {
    serve();
    renderWithQuery(<StatsScreen />);

    // 43,460,948 / 1924 ≈ 22.6k of context on every single call.
    expect(await screen.findByText("22.6k")).toBeInTheDocument();
  });

  it("separates this session from all time", async () => {
    serve();
    renderWithQuery(<StatsScreen />);

    expect(await screen.findByText(/By model, this session/)).toBeInTheDocument();
    expect(screen.getByText(/By model, all time/)).toBeInTheDocument();
    // All time includes a model whose preset is gone — its spend still happened.
    expect(await screen.findByText("a-model-since-deleted")).toBeInTheDocument();
  });

  it("puts the session's own totals at the top", async () => {
    serve();
    renderWithQuery(<StatsScreen />);

    expect(await screen.findByText("51.0M")).toBeInTheDocument();   // tokens
    expect(screen.getByText("1,928")).toBeInTheDocument();          // calls
    expect(screen.getByText("1h 02m")).toBeInTheDocument();         // working time
    expect(screen.getByText("2 done")).toBeInTheDocument();
    expect(screen.getByText(/1 failed · 4 in the plan/)).toBeInTheDocument();
  });

  it("says nothing has been asked rather than showing an empty table", async () => {
    serve({ "/api/sessions/s1/usage": { models: [], total: 0, calls: 0 } });
    renderWithQuery(<StatsScreen />);
    expect(await screen.findAllByText(/Nothing has been asked of a model yet/))
      .not.toHaveLength(0);
  });
});
