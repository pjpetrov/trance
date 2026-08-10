/** A command that is still running.
 *
 * Only its ending was ever drawn. A step blocked on `npm run dev 2>&1 &` showed
 * nothing at all for the three minutes it took the timeout to kill it — the
 * console looked idle while the run was stuck on one line, and there was no way
 * to stop it short of stopping the whole run.
 */

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RunScreen } from "@/screens/RunScreen";
import { useUi } from "@/store/ui";
import { fakeServer, renderWithQuery, stubWebSocket } from "./render";
import { eventsRoute, session, step } from "./fixtures";
import type { TranceEvent } from "@/api/types";

let nth = 0;
const at = (type: string, payload: Record<string, unknown>): TranceEvent => ({
  id: `e${(nth += 1)}`, type, session_id: "s1", step_id: "st1",
  ts: new Date().toISOString(), agent: "frontend", payload,
});

const started = () => at("command_started",
  { command_id: "cmd_1", command: "npm run dev 2>&1 &" });
const finished = () => at("command_finished",
  { command_id: "cmd_1", exit_code: 0, seconds: 4.7, timed_out: false });

const serve = (events: TranceEvent[], running = true) => fakeServer({
  "/api/sessions/s1": session({
    status: running ? "running" : "halted",
    flow: { steps: [step({ id: "st1", status: running ? "running" : "failed" })], cursor: 0 },
  }),
  "/api/sessions/s1/events": eventsRoute(events, events),
  "/api/commands/cancel/cmd_1": { cancelled: true, still_running: [] },
});

beforeEach(() => {
  stubWebSocket();
  useUi.setState({ sessionId: "s1", screen: "run", openStep: null, follow: true });
});
afterEach(() => vi.unstubAllGlobals());

describe("a command still running", () => {
  it("says what it is running and for how long", async () => {
    serve([started()]);
    renderWithQuery(<RunScreen />);

    expect(await screen.findByText("npm run dev 2>&1 &")).toBeInTheDocument();
    expect(screen.getByText(/^running \d+s$/)).toBeInTheDocument();
  });

  it("offers to stop it", async () => {
    const user = userEvent.setup();
    const server = serve([started()]);
    renderWithQuery(<RunScreen />);

    await user.click(await screen.findByRole("button", { name: "cancel" }));
    await waitFor(() => {
      const killed = server.calls.find((call) => call.method === "POST");
      expect(killed?.url).toBe("/api/commands/cancel/cmd_1");
    });
    // The confirmation is a toast, which lives in the app shell rather than
    // this screen; what matters here is that the kill actually went out.
  });

  it("stops saying so once the command ends", async () => {
    serve([started(), finished()]);
    renderWithQuery(<RunScreen />);
    await screen.findByText(/Build the maze renderer/);

    // Its tool_call line reports what it did; this one has no more to say.
    await waitFor(() =>
      expect(screen.queryByText(/^running \d+s$/)).not.toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "cancel" })).not.toBeInTheDocument();
  });

  it("does not offer to cancel a command from a run that is over", async () => {
    // The events end mid-command because the run was halted there. Nothing is
    // running, so there is nothing to stop.
    serve([started()], false);
    renderWithQuery(<RunScreen />);
    await screen.findByText(/Build the maze renderer/);

    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "cancel" })).not.toBeInTheDocument());
  });
});
