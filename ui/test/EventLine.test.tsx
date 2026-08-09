/** The console renderer.
 *
 * This is the component worth testing hardest: it is the only place a run's
 * evidence is shown, several of its cases exist because a *wrong* answer once
 * reached the user, and it is shared by the live console and the step history —
 * so a bug here is a bug in both.
 */

import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { EventLine } from "@/components/EventLine";
import { event } from "./fixtures";

const show = (detail: Parameters<typeof event>[0]) =>
  render(<EventLine event={event(detail)} sessionId="s1" />);

describe("a visual check's evidence", () => {
  it("shows the screenshot, the question and the answer together", async () => {
    show({
      kind: "screenshot", shot: "st1/001.png", clipped: true,
      question: "does the maze look right?",
      checks: ["exactly four ghosts are visible"],
      answer: "1. DESCRIBE a blue maze … only one red ghost is visible",
      prompt: "You are inspecting a screenshot …",
      preset: "qwen-local", usage: { total_tokens: 675 },
      region: { x: 0, y: 0, width: 462, height: 534 },
    });

    // Open by default: this is the one entry you cannot verify by reading code.
    expect(screen.getByText(/only one red ghost is visible/)).toBeInTheDocument();
    expect(screen.getByText("exactly four ghosts are visible")).toBeInTheDocument();

    const shot = screen.getByRole("img", { name: /vision model/i });
    expect(shot).toHaveAttribute("src", "/api/sessions/s1/shot/st1/001.png");

    // The exact prompt is available but folded — it is for when the answer
    // makes no sense and the question is the suspect.
    expect(screen.getByText(/full prompt it was sent/)).toBeInTheDocument();
  });

  it("keeps the screenshot when the model failed to answer", () => {
    show({
      kind: "screenshot", shot: "st1/002.png", question: "anything?",
      checks: [], answer: "", error: "the vision model returned an empty answer",
    });
    // Losing the picture because the model errored is the worst of both.
    expect(screen.getByRole("img", { name: /vision model/i })).toBeInTheDocument();
    expect(screen.getByText(/empty answer/)).toBeInTheDocument();
    expect(screen.getByText("no answer")).toBeInTheDocument();
  });
});

describe("a keypress", () => {
  it("says whether it arrived and whether the screen changed", () => {
    show({
      kind: "key", key: "Space", times: 1, delivered: true, changed: true, frames: 60,
      shot_before: "st1/001.png", shot_after: "st1/002.png",
      diff: { identical: false, differing: 33594, total: 246708, fraction: 0.136,
              how: "pixels", note: "",
              described: "The two screenshots differ in 33594 of 246708 pixels (13.62%)." },
    });
    expect(screen.getByText(/the screen changed/)).toBeInTheDocument();
    expect(screen.getByText(/after 60 frames/)).toBeInTheDocument();
  });

  it("distinguishes a key that never arrived from one that was ignored", () => {
    const lost = render(<EventLine event={event({
      kind: "key", key: "Space", times: 1, delivered: false, changed: null, frames: 60,
    })} sessionId="s1" />);
    expect(lost.getByText(/never reached the page/)).toBeInTheDocument();

    lost.unmount();
    show({ kind: "key", key: "Space", times: 1, delivered: true, changed: false, frames: 60 });
    expect(screen.getByText(/nothing changed/)).toBeInTheDocument();
  });

  it("opens on 'nothing changed' and shows both pictures, because that claim has been wrong",
    () => {
      // A WebGL canvas reads back empty and the digest of an empty buffer is a
      // constant, so this once said "identical" about two visibly different
      // screenshots. The pair is how a person catches that.
      show({
        kind: "key", key: "Space", times: 1, delivered: true, changed: false, frames: 60,
        shot_before: "st1/001.png", shot_after: "st1/002.png",
        diff: { identical: true, differing: 0, total: 40000, fraction: 0, how: "pixels",
                note: "", described: "The two screenshots are identical — every one of 40000 pixels matches." },
      });
      expect(screen.getByText(/40000 pixels matches/)).toBeInTheDocument();
      expect(screen.getByText("before")).toBeInTheDocument();
      expect(screen.getByText("after")).toBeInTheDocument();
      expect(screen.getAllByRole("img")).toHaveLength(2);
    });
});

describe("the cheap measurements", () => {
  it("reads a blank, frozen canvas as two separate findings", () => {
    show({
      kind: "canvas", canvas: true, canvases: 1, size: "456x528", blank: true,
      moving: false, frames: 30, note: null,
      errors: { console: ["Uncaught TypeError — app.js"], exceptions: [],
                failed_requests: [], total: 1 },
    });
    expect(screen.getByText("BLANK")).toBeInTheDocument();
    expect(screen.getByText("FROZEN")).toBeInTheDocument();
    // Open by default, and the page's own complaint is in it.
    expect(screen.getByText(/Uncaught TypeError/)).toBeInTheDocument();
  });

  it("warns that a built project served statically may be showing its failure", () => {
    show({
      kind: "page", page: "index.html", url: "http://x/", frames: 60, asked_frames: 60,
      needs_build: true, canvas: true, size: "800x600", blank: false,
      errors: { console: [], exceptions: [], failed_requests: [], total: 0 },
    });
    expect(screen.getByText(/needs a build/)).toBeInTheDocument();
  });
});

describe("ordinary work", () => {
  it("colours a diff and states the line counts", () => {
    show({
      kind: "write", path: "src/game.js", created: false, added: 12, removed: 3,
      diff: "@@ -1,2 +1,3 @@\n-old line\n+new line",
    });
    expect(screen.getByText("src/game.js")).toBeInTheDocument();
    expect(screen.getByText("+12")).toBeInTheDocument();
    expect(screen.getByText("−3")).toBeInTheDocument();
  });

  it("opens a failing command and folds a passing one", async () => {
    const user = userEvent.setup();
    const failing = render(<EventLine event={event({
      kind: "command", command: "npm test", exit_code: 1, output: "2 failing",
      seconds: 4,
    })} sessionId="s1" />);
    expect(failing.getByText("2 failing")).toBeInTheDocument();
    failing.unmount();

    show({ kind: "command", command: "npm test", exit_code: 0, output: "all good",
           seconds: 4 });
    expect(screen.queryByText("all good")).not.toBeInTheDocument();
    await user.click(screen.getByText(/exit 0/));
    expect(screen.getByText("all good")).toBeInTheDocument();
  });

  it("shows a graph miss but keeps a hit quiet", () => {
    const miss = render(<EventLine event={event({
      kind: "graph", tool: "get_definition", hit: false, query: "renderMaze",
    })} sessionId="s1" />);
    // A miss is how you notice an agent searching for something that cannot
    // exist, so it is open; a hit is noise.
    expect(within(miss.container).getByText("no match")).toBeInTheDocument();
  });
});

describe("things that went wrong", () => {
  it("names the output limit when a tool call was cut off", () => {
    show({ kind: "truncated", limit: 8000 });
    expect(screen.getByText(/8000-token output limit/)).toBeInTheDocument();
  });

  it("renders an unknown detail kind rather than dropping the line", () => {
    // A new kind added on the Python side must not silently vanish here.
    show({ kind: "look_failed", error: "no browser on this machine" });
    expect(screen.getByText(/could not run|look failed/i)).toBeInTheDocument();
  });
});
