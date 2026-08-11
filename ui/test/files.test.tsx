/** The Files screen, against the shapes the server really sends.
 *
 * This screen crashed in the user's browser on `files.data.lines.toLocaleString()`
 * — a field /api/sessions/{id}/files has never returned. The fixtures below are
 * copied from a live response, and the point of this file is that a type
 * invented rather than checked cannot pass it.
 */

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FilesScreen, buildTree } from "@/screens/FilesScreen";
import { useUi } from "@/store/ui";
import { fakeServer, renderWithQuery, stubWebSocket, type FakeRequest }
  from "./render";
import { session } from "./fixtures";

/** Verbatim from GET /api/sessions/{id}/files. Flat — there is no server tree. */
const LISTING = {
  root: "/w/chicken",
  files: [
    { path: ".gitignore", bytes: 165, lines: 7 },
    { path: "index.html", bytes: 670, lines: 22 },
    { path: "js/game.js", bytes: 12000, lines: 400 },
    { path: "js/player.js", bytes: 5000, lines: 180 },
    { path: "css/style.css", bytes: 900, lines: 40 },
  ],
  totals: [
    { ext: "js", files: 2, lines: 580, bytes: 17000 },
    { ext: "html", files: 1, lines: 22, bytes: 670 },
    { ext: "css", files: 1, lines: 40, bytes: 900 },
    { ext: "", files: 1, lines: 7, bytes: 165 },
  ],
};

/** GET /api/sessions/{id}/preview when nothing is being served: every field
 *  present and empty, not null. */
const IDLE_PREVIEW = { root: "", port: 0, url: "", public: "" };

beforeEach(() => {
  stubWebSocket();
  useUi.setState({ sessionId: "s1", screen: "files", filePath: null, modal: null });
});
afterEach(() => vi.unstubAllGlobals());

const routes = (over: Record<string, unknown> = {}) => ({
  "/api/sessions/s1": session(),
  "/api/sessions/s1/files": LISTING,
  "/api/sessions/s1/preview": IDLE_PREVIEW,
  ...over,
});

describe("buildTree", () => {
  it("groups a flat path list into folders", () => {
    const tree = buildTree(LISTING.files);
    // Folders first, then files, each alphabetical.
    expect(tree.children.map((node) => node.name)).toEqual([
      "css", "js", ".gitignore", "index.html",
    ]);
    const js = tree.children.find((node) => node.name === "js")!;
    expect(js.file).toBeUndefined();                       // a folder, not a file
    expect(js.children.map((node) => node.path)).toEqual(["js/game.js", "js/player.js"]);
    expect(js.children[0]!.file).toMatchObject({ lines: 400 });
  });

  it("handles an empty project without inventing a root file", () => {
    expect(buildTree([]).children).toEqual([]);
  });
});

describe("the files screen", () => {
  it("counts files and lines from the totals rollup", async () => {
    fakeServer(routes());
    renderWithQuery(<FilesScreen />);
    // 2 + 1 + 1 + 1 files, 580 + 22 + 40 + 7 lines. Read from `totals`, which is
    // the only place the server reports them.
    expect(await screen.findByText("5 files · 649 lines")).toBeInTheDocument();
  });

  it("renders without crashing on the real payload", async () => {
    fakeServer(routes());
    renderWithQuery(<FilesScreen />);
    expect(await screen.findByText("index.html")).toBeInTheDocument();
    expect(screen.getByText("js")).toBeInTheDocument();
  });

  it("opens a file and offers commenting on a line", async () => {
    const user = userEvent.setup();
    fakeServer(routes({
      "/api/sessions/s1/file": { path: "index.html", content: "<html>\n</html>",
                                 bytes: 670, lines: 22 },
    }));

    renderWithQuery(<FilesScreen />);
    await user.click(await screen.findByText("index.html"));
    // The subtitle also says how to comment, so match on the counts alone.
    expect(await screen.findByText(/22 lines · 670 bytes/)).toBeInTheDocument();
    expect(screen.getByText(/click a line number to comment/)).toBeInTheDocument();
  });

  it("keeps review out of the way until there is something to review", async () => {
    const user = userEvent.setup();
    fakeServer(routes());
    renderWithQuery(<FilesScreen />);

    // No panel sitting at the bottom of every visit: a general comment is a
    // button, and Finish appears only once there is something to finish.
    await screen.findByText("index.html");
    expect(screen.queryByRole("button", { name: /Review finished/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "General comment" }));
    expect(await screen.findByPlaceholderText(/about the project as a whole/))
      .toBeInTheDocument();
  });

  it("offers Finish once a comment exists, and shows a general one", async () => {
    fakeServer(routes({
      "/api/sessions/s1": session({
        review: [{ id: "rv_1", path: "", line: 0, code: "",
                   note: "the whole thing feels sluggish" }],
      }),
    }));
    renderWithQuery(<FilesScreen />);

    expect(await screen.findByText(/the whole thing feels sluggish/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Review finished \(1\)/ })).toBeInTheDocument();
  });

  it("asks how a review should be answered before sending it", async () => {
    const user = userEvent.setup();
    const server = fakeServer(routes({
      "/api/sessions/s1": session({ review: [{ id: "rv_1", path: "", line: 0, code: "", note: "too slow" }] }),
      "/api/loops": { loops: [{ name: "test-and-fix", description: "", prompt: "",
                                nodes: [], start: "", max_steps: 8 }] },
      "/api/sessions/s1/review/finish": {
        id: "rev_1", step_id: "st_new", notes: [], before: "", at: "",
        started: true, flow: { steps: [], cursor: 0 },
      },
    }));

    renderWithQuery(<FilesScreen />);
    await user.click(await screen.findByRole("button", { name: /Review finished/ }));

    await user.selectOptions(await screen.findByRole("combobox"), "test-and-fix");
    await user.click(screen.getByRole("button", { name: "Send it" }));

    await waitFor(() => {
      const sent = server.to("/api/sessions/s1/review/finish").at(-1);
      expect(sent?.body).toMatchObject({ loop: "test-and-fix" });
    });
    // Straight to where it runs, with the new step open.
    await waitFor(() => expect(useUi.getState().screen).toBe("run"));
    expect(useUi.getState().openStep).toBe("st_new");
  });

  it("offers serve static and start app where they can be seen", async () => {
    // The old entry was a ▷ hidden on each file row, opening a modal that
    // asked which kind. The two kinds are the two buttons now, in the Files
    // header, visible before any file is hovered.
    const user = userEvent.setup();
    const server = fakeServer(routes({
      "/api/sessions/s1/preview": { ...IDLE_PREVIEW },
    }));

    renderWithQuery(<FilesScreen />);
    await screen.findByText("index.html");
    await user.click(screen.getByRole("button", { name: "serve static" }));

    await waitFor(() => {
      const started = server.calls.find(
        (call) => call.method === "POST" && call.url.includes("/preview")
                  && !call.url.includes("plan"));
      expect(started).toBeTruthy();
    });
    expect(screen.queryByText("How should this be served?")).toBeNull();
  });

  it("start app shows the command and waits for a yes", async () => {
    // Running a build on this machine is not something a button decides alone:
    // the orchestrator's command is shown, and nothing runs until confirmed.
    const user = userEvent.setup();
    const server = fakeServer(routes({
      "/api/sessions/s1/preview/plan": {
        command: "npm run dev", dir: "", why: "the README says so",
        static_instead: false, read_readme: true,
      },
    }));

    renderWithQuery(<FilesScreen />);
    await screen.findByText("index.html");
    await user.click(screen.getByRole("button", { name: "start app" }));

    expect(await screen.findByText(/npm run dev/)).toBeInTheDocument();
    // Nothing has run yet.
    expect(server.calls.some((call) => call.method === "POST"
      && call.url.endsWith("/preview"))).toBe(false);

    await user.click(screen.getByRole("button", { name: "Run it" }));
    await waitFor(() => {
      const ran = server.calls.find((call) => call.method === "POST"
        && call.url.endsWith("/preview"));
      expect((ran?.body as { command: string }).command).toBe("npm run dev");
    });
  });

  it("offers nothing to share or stop when nothing is being served", async () => {
    fakeServer(routes());
    renderWithQuery(<FilesScreen />);
    await screen.findByText("index.html");
    // An idle preview reports port 0 rather than answering null; treating that
    // as "serving" would offer buttons that can only fail.
    expect(screen.queryByRole("button", { name: "share" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "stop serving" })).not.toBeInTheDocument();
  });

  it("can stop serving once something is being served", async () => {
    const user = userEvent.setup();
    const server = fakeServer(routes({
      "/api/sessions/s1/preview": {
        root: "/w", port: 36001, url: "http://x:36001/", public: "",
      },
    }));

    renderWithQuery(<FilesScreen />);
    await user.click(await screen.findByRole("button", { name: "stop serving" }));
    await waitFor(() => {
      const stopped = server.calls.find((call) => call.method === "DELETE");
      expect(stopped?.url).toContain("/preview");
    });
  });

  it("links to the preview and the public URL once one is running", async () => {
    fakeServer(routes({
      "/api/sessions/s1/preview": {
        root: "/w/chicken", port: 36001, url: "http://192.168.1.5:36001/",
        public: "https://abc.ngrok.app",
      },
    }));
    renderWithQuery(<FilesScreen />);
    expect(await screen.findByText("serving :36001")).toBeInTheDocument();
    expect(screen.getByText("public link").closest("a"))
      .toHaveAttribute("href", "https://abc.ngrok.app");
  });

  it("says a file could not be opened rather than showing an empty pane", async () => {
    const user = userEvent.setup();
    fakeServer({
      ...routes(),
      "/api/sessions/s1/file": () => { throw new Error("unused"); },
    });
    renderWithQuery(<FilesScreen />);
    await user.click(await screen.findByText("index.html"));
    // The fake server throws, which surfaces as a failed query.
    await waitFor(() =>
      expect(screen.getByText(/could not be opened/)).toBeInTheDocument());
  });
});

describe("removing a review comment", () => {
  it("addresses the note by its id, not its position", async () => {
    const user = userEvent.setup();
    const server = fakeServer(routes({
      "/api/sessions/s1": session({
        review: [
          { id: "rv_aaa", path: "", line: 0, code: "", note: "first note" },
          { id: "rv_bbb", path: "", line: 0, code: "", note: "second note" },
        ],
      }),
      "/api/sessions/s1/review/rv_bbb": { deleted: "rv_bbb", left: 1 },
    }));

    renderWithQuery(<FilesScreen />);
    const second = await screen.findByText("second note");
    await user.click(second.parentElement!.querySelector("button")!);

    // The endpoint takes a note id. Sending an index deleted nothing and
    // answered 404, so the ✕ silently did not work.
    await waitFor(() => {
      const dropped = server.calls.find((call) => call.method === "DELETE");
      expect(dropped?.url).toContain("/review/rv_bbb");
    });
  });
});

describe("editing and deleting a file", () => {
  const open = (over: Record<string, unknown> = {}) => routes({
    "/api/sessions/s1/file": { path: "index.html", content: "<html>\n</html>",
                               bytes: 670, lines: 22 },
    ...over,
  });

  const openIt = async (user: ReturnType<typeof userEvent.setup>) => {
    renderWithQuery(<FilesScreen />);
    await user.click(await screen.findByText("index.html"));
  };

  it("is read-only until Edit is pressed", async () => {
    const user = userEvent.setup();
    fakeServer(open());
    await openIt(user);

    // Reviewing is the common case, and a stray keystroke in a file an agent is
    // about to read is a change nobody meant to make.
    expect(await screen.findByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Edit" }));
    expect(await screen.findByRole("button", { name: "Save" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
  });

  it("asks before deleting, then deletes and closes the file", async () => {
    const user = userEvent.setup();
    const server = fakeServer(open({
      "/api/sessions/s1/file": (request: FakeRequest) => (request.method === "DELETE"
        ? { deleted: "index.html", committed: true }
        : { path: "index.html", content: "<html>", bytes: 6, lines: 1 }),
    }));

    await openIt(user);
    await user.click(await screen.findByRole("button", { name: "Delete file" }));

    // A dialog that can name the file and warn about a running step, rather
    // than one line the browser wrote.
    expect(await screen.findByText("Delete index.html?")).toBeInTheDocument();
    expect(screen.getByText(/recoverable from git/)).toBeInTheDocument();
    expect(server.calls.some((call) => call.method === "DELETE")).toBe(false);

    await user.click(screen.getByRole("button", { name: "Delete it" }));
    await waitFor(() => {
      const gone = server.calls.find((call) => call.method === "DELETE");
      expect(String(gone?.url)).toContain("path=index.html");
    });
    // The pane cannot keep showing a file that is no longer there.
    await waitFor(() => expect(useUi.getState().filePath).toBeNull());
  });

  it("does not delete when the dialog is dismissed", async () => {
    const user = userEvent.setup();
    const server = fakeServer(open());

    await openIt(user);
    await user.click(await screen.findByRole("button", { name: "Delete file" }));
    await user.click(await screen.findByRole("button", { name: "Cancel" }));

    expect(server.calls.some((call) => call.method === "DELETE")).toBe(false);
  });

});
