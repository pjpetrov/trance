/** The interface follows the harness.
 *
 * Each of these was a moment where the harness moved on and the screen did not:
 * a plan appeared while you were still on the chat, a run started while you
 * were still on the plan, a step finished while the console kept showing it.
 */

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useFollowHarness } from "@/hooks/useFollowHarness";
import { keys } from "@/api/queries";
import { useUi } from "@/store/ui";
import { session, step } from "./fixtures";
import { stubWebSocket } from "./render";
import type { Session, StepStatus } from "@/api/types";

let client: QueryClient;

const wrap = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={client}>{children}</QueryClientProvider>
);

/** What the socket does when the harness moves: writes the new snapshot in. */
const push = (next: Session) => client.setQueryData(keys.session("s1"), next);

const flow = (...statuses: [string, StepStatus][]): Session => session({
  id: "s1",
  status: statuses.some(([, s]) => s === "running") ? "running" : "ready",
  flow: {
    steps: statuses.map(([id, status]) => step({ id, status })),
    cursor: 0,
  },
});

beforeEach(() => {
  stubWebSocket();
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  useUi.setState({ sessionId: "s1", screen: "home", openStep: null });
});
afterEach(() => vi.unstubAllGlobals());

const start = (first: Session) => {
  push(first);
  renderHook(() => useFollowHarness("s1"), { wrapper: wrap });
};

describe("following the harness", () => {
  it("opens the plan when more work is added to one that exists", async () => {
    // The second feature request of a session: twelve steps become fourteen,
    // and the plan you are meant to look at is on another screen.
    start(flow(["st1", "done"], ["st2", "done"]));
    expect(useUi.getState().screen).toBe("home");

    push(flow(["st1", "done"], ["st2", "done"], ["st3", "pending"]));
    await waitFor(() => expect(useUi.getState().screen).toBe("plan"));
  });

  it("opens the plan when the orchestrator produces one", async () => {
    start(session({ id: "s1", flow: { steps: [], cursor: 0 } }));
    expect(useUi.getState().screen).toBe("home");        // still chatting

    push(flow(["st1", "pending"], ["st2", "pending"]));
    await waitFor(() => expect(useUi.getState().screen).toBe("plan"));
  });

  it("opens the run when one starts, wherever it was started from", async () => {
    useUi.setState({ screen: "plan" });
    start(flow(["st1", "pending"]));

    push(flow(["st1", "running"]));
    await waitFor(() => expect(useUi.getState().screen).toBe("run"));
    // And the console is already on the step that is working.
    expect(useUi.getState().openStep).toBe("st1");
  });

  it("moves to the next step as the harness does", async () => {
    useUi.setState({ screen: "run" });
    start(flow(["st1", "running"], ["st2", "pending"]));
    await waitFor(() => expect(useUi.getState().openStep).toBe("st1"));

    push(flow(["st1", "done"], ["st2", "running"]));
    await waitFor(() => expect(useUi.getState().openStep).toBe("st2"));
  });

  it("moves to the next step from wherever you are, while following", async () => {
    // The case that looked broken: the page opens on the last step that ran,
    // nothing is running yet, and then the next one starts. There used to be a
    // silent second rule — only follow when the open step is the one that was
    // running — so following was on and did nothing.
    useUi.setState({ screen: "run", follow: true });
    start(flow(["st1", "done"], ["st2", "pending"], ["st3", "pending"]));
    useUi.setState({ openStep: "st1" });          // where the run page landed

    push(flow(["st1", "done"], ["st2", "running"], ["st3", "pending"]));
    await waitFor(() => expect(useUi.getState().openStep).toBe("st2"));

    // And on to the one after it when that finishes.
    push(flow(["st1", "done"], ["st2", "done"], ["st3", "running"]));
    await waitFor(() => expect(useUi.getState().openStep).toBe("st3"));
  });

  it("does not move at all once following is off", async () => {
    useUi.setState({ screen: "run", follow: false });
    start(flow(["st1", "running"], ["st2", "pending"]));
    useUi.setState({ openStep: "st1" });

    push(flow(["st1", "done"], ["st2", "running"]));
    await new Promise((done) => setTimeout(done, 40));
    expect(useUi.getState().openStep).toBe("st1");
  });

  it("opens a mid-run session on the step that is working, without moving you", async () => {
    useUi.setState({ screen: "files" });
    start(flow(["st1", "running"]));

    // Choosing a screen is yours; what the run screen shows is not a move.
    await waitFor(() => expect(useUi.getState().openStep).toBe("st1"));
    expect(useUi.getState().screen).toBe("files");
  });

  it("does not follow one session's run into another", async () => {
    useUi.setState({ screen: "files" });
    start(flow(["st1", "pending"]));

    push({ ...flow(["other", "running"]), id: "s2" });
    await Promise.resolve();
    expect(useUi.getState().screen).toBe("files");
  });
});
