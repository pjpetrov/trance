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
import { fakeServer, renderWithQuery, stubWebSocket } from "./render";
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

  it("cannot share when nothing is being served", async () => {
    fakeServer(routes());
    renderWithQuery(<FilesScreen />);
    // An idle preview reports port 0 rather than answering null; treating that
    // as "serving" offers a share button that can only fail.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "share" })).toBeDisabled());
    expect(screen.queryByText(/preview :/)).not.toBeInTheDocument();
  });

  it("links to the preview and the public URL once one is running", async () => {
    fakeServer(routes({
      "/api/sessions/s1/preview": {
        root: "/w/chicken", port: 36001, url: "http://192.168.1.5:36001/",
        public: "https://abc.ngrok.app",
      },
    }));
    renderWithQuery(<FilesScreen />);
    expect(await screen.findByText("preview :36001")).toBeInTheDocument();
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
