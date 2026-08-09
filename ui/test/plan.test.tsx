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
  it("starts the run and lands on the first pending step", async () => {
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
    expect(useUi.getState().openStep).toBe("b");
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
  it("asks whether to keep what is there before asking the orchestrator", async () => {
    const user = userEvent.setup();
    const server = fakeServer(routes({
      "/api/sessions/s1/chat": { session: session() },
    }));

    renderWithQuery(<PlanScreen />);
    await user.click(await screen.findByRole("button", { name: "Generate" }));
    await user.click(await screen.findByRole("button", { name: "Keep what is here" }));

    await waitFor(() => {
      const asked = server.to("/api/sessions/s1/chat").at(-1);
      expect((asked?.body as { message: string }).message).toMatch(/propose_flow/);
    });
    // Keeping means the flow is not touched on the way.
    expect(server.to("/api/sessions/s1/flow")).toHaveLength(0);
  });

  it("clears the plan first when starting fresh", async () => {
    const user = userEvent.setup();
    const server = fakeServer(routes({
      "/api/sessions/s1/chat": { session: session() },
    }));

    renderWithQuery(<PlanScreen />);
    await user.click(await screen.findByRole("button", { name: "Generate" }));
    await user.click(await screen.findByRole("button", { name: /Discard and start fresh/ }));

    await waitFor(() => {
      const cleared = server.to("/api/sessions/s1/flow").at(-1);
      expect((cleared?.body as { steps: unknown[] }).steps).toEqual([]);
    });
    await waitFor(() => expect(server.to("/api/sessions/s1/chat")).toHaveLength(1));
  });
});
