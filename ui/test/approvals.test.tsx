/** The question that has the run stopped.
 *
 * The asking worked and the waiting worked; nothing drew the question. Found
 * live: an agent asking to `rm` the throwaway test script it had just been told
 * to clean up after itself, with five minutes on the clock and a console that
 * had simply gone quiet.
 */

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApprovalPrompt } from "@/components/ApprovalPrompt";
import { useUi } from "@/store/ui";
import { fakeServer, renderWithQuery, stubWebSocket } from "./render";

const ASK = {
  id: "ap_1", kind: "command", agent: "frontend", step_id: "st1",
  subject: "rm src/test_vehicle_input.js",
  detail: { programs: ["rm"], agent_has_own_list: false },
  decision: "",
  message: "frontend wants to run a command using rm, which is not on its allowlist.",
};

const serve = (pending: unknown[]) => fakeServer({
  "/api/sessions/s1/approvals": { pending, enabled: true, timeout_s: 300 },
  "/api/sessions/s1/approvals/ap_1": { ...ASK, decision: "once", widened: false },
});

beforeEach(() => {
  stubWebSocket();
  useUi.setState({ sessionId: "s1" });
});
afterEach(() => vi.unstubAllGlobals());

describe("a run waiting on a question", () => {
  it("draws nothing when nothing is asking", async () => {
    serve([]);
    const { container } = renderWithQuery(<ApprovalPrompt />);
    await new Promise((done) => setTimeout(done, 30));
    expect(container).toBeEmptyDOMElement();
  });

  it("says what is being asked for, and what happens if ignored", async () => {
    serve([ASK]);
    renderWithQuery(<ApprovalPrompt />);

    expect(await screen.findByText(/not on its allowlist/)).toBeInTheDocument();
    expect(screen.getByText("rm src/test_vehicle_input.js")).toBeInTheDocument();
    expect(screen.getByText(/Left alone it is refused/)).toBeInTheDocument();
  });

  it("answers once", async () => {
    const user = userEvent.setup();
    const server = serve([ASK]);
    renderWithQuery(<ApprovalPrompt />);

    await user.click(await screen.findByRole("button", { name: "Allow once" }));
    await waitFor(() => {
      const sent = server.calls.find((call) => call.method === "POST");
      expect(sent?.url).toBe("/api/sessions/s1/approvals/ap_1");
      expect(sent?.body).toEqual({ decision: "once" });
    });
  });

  it("offers to widen the allowlist, naming the program", async () => {
    const user = userEvent.setup();
    const server = serve([ASK]);
    renderWithQuery(<ApprovalPrompt />);

    await user.click(await screen.findByRole("button", { name: "Always allow rm" }));
    await waitFor(() => {
      const sent = server.calls.find((call) => call.method === "POST");
      expect(sent?.body).toEqual({ decision: "always" });
    });
  });

  it("refuses", async () => {
    const user = userEvent.setup();
    const server = serve([ASK]);
    renderWithQuery(<ApprovalPrompt />);

    await user.click(await screen.findByRole("button", { name: "Refuse" }));
    await waitFor(() => {
      const sent = server.calls.find((call) => call.method === "POST");
      expect(sent?.body).toEqual({ decision: "deny" });
    });
  });

  it("says how many others are waiting behind it", async () => {
    serve([ASK, { ...ASK, id: "ap_2", subject: "rm -rf build" }]);
    renderWithQuery(<ApprovalPrompt />);
    expect(await screen.findByText(/1 more waiting/)).toBeInTheDocument();
  });
});
