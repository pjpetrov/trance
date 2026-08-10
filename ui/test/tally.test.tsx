/** What an agent turn actually spent itself on.
 *
 * Measured on one real step: the visual tester made 64 tool calls — open the
 * page, press a key, look — and the repair agent it handed to made 407, of
 * which 386 were lookups and 15 were edits. None of that was answerable from
 * the console: "show reads" changed its own label and nothing else, and a
 * folded block said what the agent concluded but never what it did.
 */

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RunScreen } from "@/screens/RunScreen";
import { useUi } from "@/store/ui";
import { isLookup, tallyOf } from "@/lib/runs";
import { fakeServer, renderWithQuery, stubWebSocket } from "./render";
import { eventsRoute, session, step } from "./fixtures";
import type { ToolDetail, TranceEvent } from "@/api/types";

let nth = 0;
const call = (name: string, detail: ToolDetail): TranceEvent => ({
  id: `e${(nth += 1)}`, type: "tool_call", session_id: "s1", step_id: "st1",
  ts: "2026-08-10T09:00:00Z", agent: "frontend",
  payload: { name, ok: true, detail },
});

const LOOKUP = () => call("get_definition",
  { kind: "graph", tool: "get_definition", hit: true, query: "GameScene.create" });
const READ = () => call("read_file", { kind: "read", path: "js/game.js", lines: 40 });
const EDIT = () => call("edit_file",
  { kind: "write", path: "js/game.js", added: 3, removed: 1, diff: "" });
const RUN = () => call("run_command",
  { kind: "command", command: "npm test", exit_code: 0, output: "ok", seconds: 1 });

beforeEach(() => {
  stubWebSocket();
  useUi.setState({ sessionId: "s1", screen: "run", openStep: null, showReads: false });
});
afterEach(() => vi.unstubAllGlobals());

describe("the tally", () => {
  it("counts what a turn was made of", () => {
    expect(tallyOf([LOOKUP(), LOOKUP(), READ(), EDIT(), RUN()]))
      .toBe("3 lookups · 1 edit · 1 command");
  });

  it("says nothing about a turn that did nothing", () => {
    expect(tallyOf([])).toBe("");
  });

  it("knows a lookup from work", () => {
    expect(isLookup(LOOKUP())).toBe(true);
    expect(isLookup(READ())).toBe(true);
    expect(isLookup(EDIT())).toBe(false);
    expect(isLookup(RUN())).toBe(false);
  });
});

describe("hiding reads", () => {
  const events = [LOOKUP(), LOOKUP(), READ(), EDIT(), RUN()];

  it("actually hides them, and says how many", async () => {
    const user = userEvent.setup();
    fakeServer({
      "/api/sessions/s1": session({
        status: "running", flow: { steps: [step({ status: "running" })], cursor: 0 },
      }),
      "/api/sessions/s1/events": eventsRoute(events, events),
    });

    renderWithQuery(<RunScreen />);
    // The edit and the command survive; the three lookups do not.
    await waitFor(() => expect(screen.getByText(/js\/game.js/)).toBeInTheDocument());
    expect(screen.queryByText(/GameScene.create/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "show reads (3)" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "show reads (3)" }));
    await waitFor(() =>
      expect(screen.getAllByText(/GameScene.create/).length).toBeGreaterThan(0));
    expect(screen.getByRole("button", { name: "hide reads" })).toBeInTheDocument();
  });
});
