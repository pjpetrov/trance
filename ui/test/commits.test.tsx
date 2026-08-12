/** From a request to the commits it produced.
 *
 * A request becomes a plan, the plan becomes a run, the run becomes commits.
 * Each was visible on its own screen and none was connected to the one before,
 * so "what came of what I asked for" was answered by remembering.
 */

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HomeScreen } from "@/screens/HomeScreen";
import { CommitsScreen } from "@/screens/CommitsScreen";
import { useUi } from "@/store/ui";
import { fakeServer, renderWithQuery, stubWebSocket } from "./render";
import { Toaster } from "@/components/Toaster";
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

const HISTORY = { requests: [{
  reply_id: "m2", ts: "2026-08-10T12:00:00Z", request: "add a level select",
  base: "abc123", after: "def456", commit_count: 1, file_count: 1,
  still_to_run: 0, worked_seconds: 754, shots: [],
}] };

beforeEach(() => {
  stubWebSocket();
  useUi.setState({ sessionId: "s1", screen: "home", commitsFor: null });
});
afterEach(() => vi.unstubAllGlobals());

describe("from a reply to its commits", () => {
  it("has a plain git-log mode beside the by-request one", async () => {
    // The page's name says commits; what it showed was iterations. Both are
    // real questions, so both are modes — and the log includes commits no
    // request owns: the user's own, and the clears.
    const user = userEvent.setup();
    useUi.setState({ screen: "commits", commitsFor: "m2" });
    fakeServer({
      "/api/sessions/s1": session({ chat: CHAT }),
      "/api/sessions/s1/requests": HISTORY,
      "/api/sessions/s1/messages/m2/commits": ANSWER,
      "/api/sessions/s1/commits": { commits: [
        { sha: "aaa111", short: "aaa111", subject: "user: cleared the generated files",
          when: "1 minute ago", who: "petrovs" },
        { sha: "def456", short: "def456", subject: "frontend: add level select",
          when: "2 hours ago", who: "trance" },
      ] },
    });
    renderWithQuery(<CommitsScreen />);
    await screen.findByText(/What each request became/);

    await user.click(screen.getByRole("button", { name: "All commits" }));
    expect(await screen.findByText(/Every commit/)).toBeInTheDocument();
    expect(screen.getByText(/cleared the generated files/)).toBeInTheDocument();
    expect(screen.getByText(/add level select/)).toBeInTheDocument();

    // And back, without losing the request that was open.
    await user.click(screen.getByRole("button", { name: "By request" }));
    expect(await screen.findByText(/What each request became/)).toBeInTheDocument();
  });

  it("says how long this iteration was worked on", async () => {
    // Summed from the request's own steps — a request that reused old work is
    // not billed for it, and the whole-session clock lives on Statistics.
    useUi.setState({ screen: "commits", commitsFor: "m2" });
    fakeServer({
      "/api/sessions/s1": session({ chat: CHAT }),
      "/api/sessions/s1/requests": HISTORY,
      "/api/sessions/s1/messages/m2/commits": ANSWER,
    });
    renderWithQuery(<CommitsScreen />);
    expect(await screen.findByText(/worked 12m 34s/)).toBeInTheDocument();
  });

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
      "/api/sessions/s1/requests": HISTORY,
      "/api/sessions/s1/messages/m2/commits": ANSWER,
    });
    renderWithQuery(<CommitsScreen />);

    // The request, because that is the thing you recognise.
    expect(await screen.findByText("add a level select")).toBeInTheDocument();
    // The detail loads a beat later — the expanded card fetches it on its own.
    expect(await screen.findByText("frontend: add level select")).toBeInTheDocument();
    expect(screen.getByText("src/menu.js")).toBeInTheDocument();
    expect(screen.getByText(/1 commit/)).toBeInTheDocument();
  });

  it("opens a commit onto its diff, and asks for it only then", async () => {
    const user = userEvent.setup();
    useUi.setState({ screen: "commits", commitsFor: "m2" });
    const server = fakeServer({
      "/api/sessions/s1": session({ chat: CHAT }),
      "/api/sessions/s1/requests": HISTORY,
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

  it("offers to run the steps this request added, and only while some are pending", async () => {
    const user = userEvent.setup();
    useUi.setState({ screen: "commits", commitsFor: "m2" });
    const server = fakeServer({
      "/api/sessions/s1": session({ chat: CHAT }),
      "/api/sessions/s1/requests": HISTORY,
      "/api/sessions/s1/messages/m2/commits": {
        ...ANSWER, still_to_run: 1,
        steps: [step({ id: "st1", task: "add level select", status: "pending" })],
      },
      "/api/sessions/s1/start": session(),
    });
    renderWithQuery(<CommitsScreen />);

    await user.click(await screen.findByRole("button", { name: /Run 1 pending/ }));
    await waitFor(() => expect(server.to("/api/sessions/s1/start")).toHaveLength(1));
    await waitFor(() => expect(useUi.getState().screen).toBe("run"));
  });

  it("does not offer to run work that has already been done", async () => {
    useUi.setState({ screen: "commits", commitsFor: "m2" });
    fakeServer({
      "/api/sessions/s1": session({ chat: CHAT }),
      "/api/sessions/s1/requests": HISTORY,
      "/api/sessions/s1/messages/m2/commits": ANSWER,   // its one step is done
    });
    renderWithQuery(<CommitsScreen />);
    await screen.findByText("frontend: add level select");
    expect(screen.queryByRole("button", { name: /Run/ })).not.toBeInTheDocument();
  });

  it("opens on the newest request when you arrive from the tab", async () => {
    // No reply was clicked — the tab is the way in, and it lands on the request
    // you would have picked.
    useUi.setState({ screen: "commits", commitsFor: null });
    fakeServer({
      "/api/sessions/s1": session({ chat: CHAT }),
      "/api/sessions/s1/requests": HISTORY,
      "/api/sessions/s1/messages/m2/commits": ANSWER,
    });
    renderWithQuery(<CommitsScreen />);
    expect(await screen.findByText("add a level select")).toBeInTheDocument();
  });

  it("says the work has not finished rather than showing nothing", async () => {
    useUi.setState({ screen: "commits", commitsFor: "m2" });
    fakeServer({
      "/api/sessions/s1": session({ chat: CHAT }),
      "/api/sessions/s1/requests": { requests: [{
        ...HISTORY.requests[0], commit_count: 0, file_count: 0, still_to_run: 2,
      }] },
      "/api/sessions/s1/messages/m2/commits": {
        ...ANSWER, commits: [], files: [], still_to_run: 2,
        steps: [step({ id: "st1", task: "add level select", status: "running" })],
      },
    });
    renderWithQuery(<CommitsScreen />);

    expect(await screen.findByText(/Nothing committed yet/)).toBeInTheDocument();
    expect(screen.getByText(/2 step\(s\) still to run/)).toBeInTheDocument();
  });


  it("clicking a changed file opens it on the files page", async () => {
    const user = userEvent.setup();
    useUi.setState({ screen: "commits", commitsFor: "m2" });
    fakeServer({
      "/api/sessions/s1": session({ chat: CHAT }),
      "/api/sessions/s1/requests": HISTORY,
      "/api/sessions/s1/messages/m2/commits": ANSWER,
    });
    renderWithQuery(<CommitsScreen />);

    await user.click(await screen.findByText("src/menu.js"));
    expect(useUi.getState().screen).toBe("files");
    expect(useUi.getState().filePath).toBe("src/menu.js");
  });

  it("rewinding asks first, then posts, then reports the kept branch", async () => {
    const user = userEvent.setup();
    useUi.setState({ screen: "commits", commitsFor: "m2" });
    const server = fakeServer({
      "/api/sessions/s1": session({ chat: CHAT }),
      "/api/sessions/s1/requests": HISTORY,
      "/api/sessions/s1/messages/m2/commits": ANSWER,
      "/api/sessions/s1/messages/m2/rewind":
        { to: "def456ab", kept_branch: "trance/pre-rewind-x", trimmed_messages: 1 },
    });
    renderWithQuery(<CommitsScreen />);

    await user.click(await screen.findByRole("button", { name: /rewind here/ }));
    // Nothing sent yet — the confirmation stands between the click and the reset.
    expect(server.to("/api/sessions/s1/messages/m2/rewind")).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Rewind" }));
    await waitFor(() =>
      expect(server.to("/api/sessions/s1/messages/m2/rewind")).toHaveLength(1));
  });

  it("run this version posts and opens the served URL", async () => {
    const user = userEvent.setup();
    const opened: string[] = [];
    vi.stubGlobal("open", (url: string) => { opened.push(url); return null; });
    useUi.setState({ screen: "commits", commitsFor: "m2" });
    const server = fakeServer({
      "/api/sessions/s1": session({ chat: CHAT }),
      "/api/sessions/s1/requests": HISTORY,
      "/api/sessions/s1/messages/m2/commits": ANSWER,
      // Nothing served until the button asks for it: GET answers empty, POST
      // answers with the started version.
      "/api/sessions/s1/preview": (call: { method: string }) =>
        call.method === "POST"
          ? { open: "http://127.0.0.1:4173/", url: "http://127.0.0.1:4173/",
              version: "def456ab", of_message: "m2", root: "/x", port: 4173,
              public: "" }
          : { root: "", port: 0, url: "", public: "" },
    });
    renderWithQuery(<CommitsScreen />);

    await user.click(await screen.findByRole("button", { name: /run this version/ }));
    await waitFor(() => expect(
      server.calls.filter((c) => c.method === "POST"
        && c.url.includes("/preview"))).toHaveLength(1));
    await waitFor(() => expect(opened).toEqual(["http://127.0.0.1:4173/"]));
  });

  it("a plan step opens on the run page, its pencil on the plan page", async () => {
    const user = userEvent.setup();
    useUi.setState({ screen: "commits", commitsFor: "m2" });
    fakeServer({
      "/api/sessions/s1": session({ chat: CHAT }),
      "/api/sessions/s1/requests": HISTORY,
      "/api/sessions/s1/messages/m2/commits": ANSWER,
    });
    renderWithQuery(<CommitsScreen />);

    await user.click(await screen.findByTitle(
      "Open this step on the run page — its history, its console"));
    expect(useUi.getState().screen).toBe("run");
    expect(useUi.getState().openStep).toBe("st1");

    useUi.setState({ screen: "commits" });
    await user.click(await screen.findByTitle("Edit this step on the plan page"));
    expect(useUi.getState().screen).toBe("plan");
    expect(useUi.getState().planFocus).toBe("st1");
  });

  it("a served version shows the same stop and share the files page has", async () => {
    const user = userEvent.setup();
    useUi.setState({ screen: "commits", commitsFor: "m2" });
    const server = fakeServer({
      "/api/sessions/s1": session({ chat: CHAT }),
      "/api/sessions/s1/requests": HISTORY,
      "/api/sessions/s1/messages/m2/commits": ANSWER,
      "/api/sessions/s1/preview": (call: { method: string }) =>
        call.method === "DELETE"
          ? { stopped: true }
          : { root: "/x", port: 4173, url: "http://127.0.0.1:4173/",
              open: "http://127.0.0.1:4173/", public: "",
              version: "def456ab", of_message: "m2" },
    });
    renderWithQuery(<CommitsScreen />);

    // The status says this very iteration is being served, so the card offers
    // stop and share instead of another start.
    expect(await screen.findByText(/serving def456ab/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "stop" }));
    await waitFor(() => expect(
      server.calls.some((c) => c.method === "DELETE"
        && c.url.includes("/preview"))).toBe(true));
  });


  it("share still answers when the clipboard API does not exist", async () => {
    // navigator.clipboard is undefined over plain http on the LAN — exactly
    // where trance is used. The link must reach the user anyway.
    const user = userEvent.setup();
    useUi.setState({ screen: "commits", commitsFor: "m2" });
    fakeServer({
      "/api/sessions/s1": session({ chat: CHAT }),
      "/api/sessions/s1/requests": HISTORY,
      "/api/sessions/s1/messages/m2/commits": ANSWER,
      "/api/sessions/s1/preview": (call: { method: string }) =>
        call.method === "POST"
          ? { url: "" }
          : { root: "/x", port: 4173, url: "http://127.0.0.1:4173/",
              open: "http://127.0.0.1:4173/", public: "",
              version: "def456ab", of_message: "m2" },
      "/api/sessions/s1/share": { url: "https://tunnel.example/abc" },
    });
    const bare = { ...navigator } as Navigator;
    Object.defineProperty(bare, "clipboard", { value: undefined });
    vi.stubGlobal("navigator", bare);

    renderWithQuery(<><CommitsScreen /><Toaster /></>);
    await user.click(await screen.findByRole("button", { name: "share" }));
    expect(await screen.findByText(/tunnel\.example\/abc/)).toBeInTheDocument();
  });
});