/** From a request to the commits it produced.
 *
 * A request becomes a plan, the plan becomes a run, the run becomes commits.
 * Each was visible on its own screen and none was connected to the one before,
 * so "what came of what I asked for" was answered by remembering.
 */

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HomeScreen } from "@/screens/HomeScreen";
import { CommitsScreen } from "@/screens/CommitsScreen";
import { useUi } from "@/store/ui";
import { fakeServer, renderWithQuery, stubWebSocket } from "./render";
import { session, step } from "./fixtures";

const CHAT = [
  { id: "m1", role: "user", content: "add a level select", images: [] },
  { id: "m2", role: "orchestrator", content: "I will add the level select.",
    images: [], base: "abc123", steps: ["st1"] },
  { id: "m3", role: "user", content: "thanks", images: [] },
];

const ANSWER = {
  message: { id: "m2", role: "orchestrator",
             content: "I will add the level select.", ts: "2026-08-10T12:00:00Z" },
  base: "abc123",
  after: "def456",
  steps: [step({ id: "st1", task: "add level select", status: "done" })],
  still_to_run: 0,
  commits: [{ sha: "def456", short: "def456", subject: "frontend: add level select",
              when: "2 minutes ago", who: "trance" }],
  files: ["src/menu.js"],
};

beforeEach(() => {
  stubWebSocket();
  useUi.setState({ sessionId: "s1", screen: "home", commitsFor: null });
});
afterEach(() => vi.unstubAllGlobals());

describe("from a reply to its commits", () => {
  it("offers the way through only on a reply that proposed work", async () => {
    fakeServer({
      "/api/sessions": [],
      "/api/sessions/s1": session({ chat: CHAT }),
    });
    renderWithQuery(<HomeScreen />);
    await screen.findByText(/I will add the level select/);

    // One button, on the reply that has a base — not on the two plain messages.
    expect(screen.getAllByRole("button", { name: /what came of this/ }))
      .toHaveLength(1);
  });

  it("takes you to the commits that reply produced", async () => {
    const user = userEvent.setup();
    fakeServer({
      "/api/sessions": [],
      "/api/sessions/s1": session({ chat: CHAT }),
    });
    renderWithQuery(<HomeScreen />);
    await screen.findByText(/I will add the level select/);

    await user.click(screen.getByRole("button", { name: /what came of this/ }));
    expect(useUi.getState().screen).toBe("commits");
    expect(useUi.getState().commitsFor).toBe("m2");
  });

  it("shows the request, the steps and the commits", async () => {
    useUi.setState({ screen: "commits", commitsFor: "m2" });
    fakeServer({
      "/api/sessions/s1": session({ chat: CHAT }),
      "/api/sessions/s1/messages/m2/commits": ANSWER,
    });
    renderWithQuery(<CommitsScreen />);

    // The request, because that is the thing you recognise.
    expect(await screen.findByText("add a level select")).toBeInTheDocument();
    expect(screen.getByText("frontend: add level select")).toBeInTheDocument();
    expect(screen.getByText("src/menu.js")).toBeInTheDocument();
    expect(screen.getByText(/1 commit/)).toBeInTheDocument();
  });

  it("opens a commit onto its diff, and asks for it only then", async () => {
    const user = userEvent.setup();
    useUi.setState({ screen: "commits", commitsFor: "m2" });
    const server = fakeServer({
      "/api/sessions/s1": session({ chat: CHAT }),
      "/api/sessions/s1/messages/m2/commits": ANSWER,
      "/api/sessions/s1/commit/def456": {
        sha: "def456", short: "def456", subject: "frontend: add level select",
        when: "2 minutes ago", who: "trance",
        stat: " src/menu.js | 4 ++++", diff: "+++ b/src/menu.js\n+the new menu",
        clipped: false,
      },
    });
    renderWithQuery(<CommitsScreen />);
    await screen.findByText("frontend: add level select");

    expect(server.calls.some((call) => call.url.includes("/commit/"))).toBe(false);
    await user.click(screen.getByText("frontend: add level select"));
    expect(await screen.findByText(/the new menu/)).toBeInTheDocument();
  });

  it("says the work has not finished rather than showing nothing", async () => {
    useUi.setState({ screen: "commits", commitsFor: "m2" });
    fakeServer({
      "/api/sessions/s1": session({ chat: CHAT }),
      "/api/sessions/s1/messages/m2/commits": {
        ...ANSWER, commits: [], files: [], still_to_run: 2,
        steps: [step({ id: "st1", task: "add level select", status: "running" })],
      },
    });
    renderWithQuery(<CommitsScreen />);

    expect(await screen.findByText(/Nothing committed yet/)).toBeInTheDocument();
    expect(screen.getByText(/2 step\(s\) still to run/)).toBeInTheDocument();
  });
});
