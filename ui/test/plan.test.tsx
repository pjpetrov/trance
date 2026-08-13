/** The plan screen: adding, deleting, folding what has run, and generating.
 *
 * The rule underneath most of it: a step that already ran is a record, not
 * clutter. It folds away rather than disappearing, because the easiest way to
 * write the next step is to copy one that worked.
 */

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PlanScreen } from "@/screens/PlanScreen";
import { useUi } from "@/store/ui";
import { fakeServer, renderWithQuery, stubWebSocket } from "./render";
import { role, session, step } from "./fixtures";

const routes = (over: Record<string, unknown> = {}) => ({
  "/api/sessions/s1": session(),
  "/api/agents": { agents: [role({ name: "frontend" })], verifiers: [], toolsets: [] },
  "/api/loops": { loops: [] },
  "/api/sessions/s1/flow": { steps: [], team: [] },
  ...over,
});

beforeEach(() => {
  stubWebSocket();
  useUi.setState({ sessionId: "s1", screen: "plan", openStep: null });
});
afterEach(() => vi.unstubAllGlobals());

describe("adding a step", () => {
  it("adds an empty one to write in, with no text needed first", async () => {
    const user = userEvent.setup();
    const server = fakeServer(routes());

    renderWithQuery(<PlanScreen />);
    await user.click(await screen.findByRole("button", { name: "Add a step" }));

    // The old form made you type into a one-line box before a step existed.
    await waitFor(() => {
      const saved = server.to("/api/sessions/s1/flow").at(-1);
      const steps = (saved?.body as { steps: { task: string }[] }).steps;
      expect(steps).toHaveLength(2);
      expect(steps.at(-1)!.task).toBe("");
    });
  });
});

describe("steps that already ran", () => {
  it("folds them away but says they are there and can be reused", async () => {
    const user = userEvent.setup();
    fakeServer(routes({
      "/api/sessions/s1": session({
        flow: {
          steps: [
            step({ id: "a", task: "Set the project up", status: "done" }),
            step({ id: "b", task: "Build the renderer", status: "failed" }),
            step({ id: "c", task: "Still to do", status: "pending" }),
          ],
          cursor: 0,
        },
      }),
    }));

    renderWithQuery(<PlanScreen />);
    expect(await screen.findByDisplayValue("Still to do")).toBeInTheDocument();
    // Cleared from the list, not from existence.
    expect(screen.queryByDisplayValue("Set the project up")).not.toBeInTheDocument();
    expect(screen.getByText(/2 steps already ran/)).toBeInTheDocument();
    expect(screen.getByText("1 done")).toBeInTheDocument();
    expect(screen.getByText("1 failed")).toBeInTheDocument();

    await user.click(screen.getByText(/2 steps already ran/));
    expect(await screen.findByDisplayValue("Set the project up")).toBeInTheDocument();
  });
});

describe("running from the plan", () => {
  it("starts the run and goes to the run page", async () => {
    const user = userEvent.setup();
    const server = fakeServer(routes({
      "/api/sessions/s1": session({
        flow: {
          steps: [step({ id: "a", status: "done" }), step({ id: "b", status: "pending" })],
          cursor: 0,
        },
      }),
      "/api/sessions/s1/start": session(),
    }));

    renderWithQuery(<PlanScreen />);
    await user.click(await screen.findByRole("button", { name: "Run" }));

    await waitFor(() => expect(server.to("/api/sessions/s1/start")).toHaveLength(1));
    await waitFor(() => expect(useUi.getState().screen).toBe("run"));
    // Which step is open is the run page's decision — it picks whatever is
    // running, else the last that ran. Deciding here too meant the first
    // pending step won over the one that actually started.
  });

  it("will not run when nothing is pending", async () => {
    fakeServer(routes({
      "/api/sessions/s1": session({
        flow: { steps: [step({ id: "a", status: "done" })], cursor: 0 },
      }),
    }));
    renderWithQuery(<PlanScreen />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Run" })).toBeDisabled());
  });
});

describe("generating a plan", () => {
  it("appends straight away, with nothing to confirm and nothing cleared", async () => {
    // The old button opened a modal whose real offer was "discard everything
    // first". Appending never destroys — run steps are untouched and proposals
    // matching finished work are skipped server-side — so there is nothing to
    // ask permission for, and the destructive branch is gone with the modal.
    const user = userEvent.setup();
    const server = fakeServer(routes({
      "/api/sessions/s1/chat": { session: session() },
    }));

    renderWithQuery(<PlanScreen />);
    await user.click(await screen.findByRole("button", { name: "Regenerate last request" }));

    expect(screen.queryByText("Ask the orchestrator for a plan")).toBeNull();
    await waitFor(() => {
      const asked = server.to("/api/sessions/s1/chat").at(-1);
      expect((asked?.body as { message: string }).message).toMatch(/propose_flow/);
    });
    // Nothing was cleared on the way.
    expect(server.to("/api/sessions/s1/flow")).toHaveLength(0);
  });
});

describe("editing survives the server's echo", () => {
  it("does not stomp a task being typed when a session update arrives", async () => {
    // The old shape: every incoming session update replaced the local steps
    // wholesale, and the engine touches the session constantly during a run —
    // so an edit visibly reverted, then re-applied when its save landed.
    const user = userEvent.setup();
    let clock = 0;
    const server = fakeServer(routes({
      // Always answers the stale copy, and never a deep-equal one: the run
      // clock ticks on every touch in production, and react-query's
      // structural sharing keeps the old identity for identical answers —
      // which would leave the guard unexercised.
      "/api/sessions/s1": () => session({
        run_seconds: ++clock,
        flow: { steps: [step({ id: "st1", task: "old words", status: "pending" })],
                cursor: 0 },
      }),
    }));
    const { client } = renderWithQuery(<PlanScreen />);

    const area = await screen.findByDisplayValue("old words");
    await user.clear(area);
    await user.type(area, "the new task");

    // A background refetch lands mid-edit — exactly what a run's touch does.
    const asked = () =>
      server.calls.filter((c) => c.url.startsWith("/api/sessions/s1")).length;
    const before = asked();
    await client.invalidateQueries();
    await waitFor(() => expect(asked()).toBeGreaterThan(before));
    // Give the would-be stomp its beat to land before asserting it did not.
    await new Promise((settle) => setTimeout(settle, 60));
    expect(screen.getByDisplayValue("the new task")).toBeInTheDocument();
  });
});
