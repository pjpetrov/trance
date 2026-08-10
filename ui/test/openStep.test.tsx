/** Arriving on the run page.
 *
 * It used to arrive on nothing: no step open, so the console fell back to the
 * session-wide feed — lines from whichever agent was mid-sentence, with no way
 * to tell which step they came from, and the rail showing nothing as selected
 * while a step was plainly running. Opening the page means "show me the work",
 * and the work is a step.
 */

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RunScreen } from "@/screens/RunScreen";
import { useUi } from "@/store/ui";
import { fakeServer, renderWithQuery, stubWebSocket } from "./render";
import { session, step } from "./fixtures";
import type { StepStatus } from "@/api/types";

const plan = (...rows: [string, StepStatus, number][]) => session({
  id: "s1",
  status: rows.some(([, s]) => s === "running") ? "running" : "ready",
  flow: {
    steps: rows.map(([id, status, runs]) => step({ id, status, runs })),
    cursor: 0,
  },
});

const serve = (body: ReturnType<typeof plan>) => fakeServer({
  "/api/sessions/s1": body,
  "/api/sessions/s1/events": [],
});

beforeEach(() => {
  stubWebSocket();
  useUi.setState({ sessionId: "s1", screen: "run", openStep: null });
});
afterEach(() => vi.unstubAllGlobals());

describe("opening the run page", () => {
  it("selects the step that is running", async () => {
    serve(plan(["st1", "done", 1], ["st2", "running", 4]));
    renderWithQuery(<RunScreen />);
    await waitFor(() => expect(useUi.getState().openStep).toBe("st2"));
  });

  it("falls back to the last step that actually ran", async () => {
    // Nothing is running — a halted or finished flow. The last step with a run
    // behind it is the one you came to look at, not the first pending one.
    serve(plan(["st1", "done", 1], ["st2", "failed", 3], ["st3", "pending", 0]));
    renderWithQuery(<RunScreen />);
    await waitFor(() => expect(useUi.getState().openStep).toBe("st2"));
  });

  it("opens a plan that has never run on its last step", async () => {
    serve(plan(["st1", "pending", 0], ["st2", "pending", 0]));
    renderWithQuery(<RunScreen />);
    await waitFor(() => expect(useUi.getState().openStep).toBe("st2"));
  });

  it("picks again each time you come back, not once per session", async () => {
    // Returning to this page asks the same question again, and the answer has
    // usually changed — the step you left open has finished and another is
    // running. Within a visit it still never fights a click.
    useUi.setState({ openStep: "st1" });
    serve(plan(["st1", "done", 1], ["st2", "running", 4]));
    renderWithQuery(<RunScreen />);
    await waitFor(() => expect(useUi.getState().openStep).toBe("st2"));
  });

  it("keeps the open step open when you click it again", async () => {
    // It used to toggle, and closing it dropped the console back to a
    // session-wide feed — a state nobody chose and nothing announced.
    const user = userEvent.setup();
    serve(plan(["st1", "done", 1], ["st2", "running", 4]));
    renderWithQuery(<RunScreen />);
    await waitFor(() => expect(useUi.getState().openStep).toBe("st2"));

    await user.click(screen.getAllByText(/Build the maze renderer/)[1]!);
    expect(useUi.getState().openStep).toBe("st2");
  });

  it("waits for the plan rather than giving up on an empty one", async () => {
    // A session opened before its plan exists must not burn its one pick on
    // an empty list and then never choose again.
    let steps = session({ id: "s1", flow: { steps: [], cursor: 0 } });
    fakeServer({
      "/api/sessions/s1": () => steps,
      "/api/sessions/s1/events": [],
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><RunScreen /></QueryClientProvider>);

    await screen.findByText(/No steps yet/);
    expect(useUi.getState().openStep).toBeNull();

    // The steps arrive a moment later, as they do when a plan is generated.
    steps = plan(["st1", "running", 1]);
    await client.invalidateQueries();
    await waitFor(() => expect(useUi.getState().openStep).toBe("st1"));
  });
});
