/** The loops modal's routing, choosable rather than hardcoded.
 *
 * The old render was read-only in disguise: it listed only the routes a node
 * already had, so an outcome could be re-aimed but never routed, tiered or
 * unrouted — and the visit cap was a label, not an input.
 */

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LoopsEditor } from "@/modals/LoopsEditor";
import { useUi } from "@/store/ui";
import { fakeServer, renderWithQuery, stubWebSocket } from "./render";
import { role } from "./fixtures";

const LOOP = {
  name: "test-and-fix",
  description: "", prompt: "", max_steps: 10, start: "n1",
  nodes: [{
    id: "n1", role: "tester", focus: "", check: null, checks: [],
    checks_seeded: true, revert_on_fail: false,
    on: { SUCCESS: [{ target: "n1", max_visits: 3 }] },
  }],
};

beforeEach(() => {
  stubWebSocket();
  useUi.setState({ sessionId: "s1" });
  fakeServer({
    "/api/loops": { loops: [LOOP], outcomes: {}, stops: [], agents: [], verifiers: [] },
    "/api/agents": { agents: [role({ name: "tester" }), role({ name: "backend" })],
                     verifiers: [], toolsets: [] },
  });
});
afterEach(() => vi.unstubAllGlobals());

describe("the loops modal's routing", () => {
  it("shows every outcome, routed or not, and lets an unrouted one be routed", async () => {
    const user = userEvent.setup();
    renderWithQuery(<LoopsEditor />);
    await screen.findByDisplayValue("test-and-fix");

    // FAILED has no route: said as the halt it is, not hidden.
    expect(screen.getAllByText("halts the loop").length).toBeGreaterThan(0);

    // The + on an unrouted outcome adds a route, which is now editable.
    const failedRow = screen.getByTitle("the agent reported failure").parentElement!;
    await user.click(within(failedRow).getByRole("button", { name: "+" }));
    expect(within(failedRow).getByRole("combobox")).toBeInTheDocument();
    // A pending change, ready to Apply.
    expect(screen.getByText(/1 unsaved change/)).toBeInTheDocument();
  });

  it("makes the visit cap an input and a route removable", async () => {
    const user = userEvent.setup();
    renderWithQuery(<LoopsEditor />);
    await screen.findByDisplayValue("test-and-fix");

    const successRow = screen.getByTitle("the agent reported success").parentElement!;
    const cap = within(successRow).getByRole("spinbutton");
    expect(cap).toHaveValue(3);

    await user.click(within(successRow).getByTitle("Remove this route"));
    expect(within(successRow).getByText("halts the loop")).toBeInTheDocument();
  });
});
