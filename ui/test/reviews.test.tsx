/** The reviews page and the line-comment flow.
 *
 * The shapes here come from the endpoints: /reviews answers summaries with
 * commits, and a patch arrives only when a commit is opened.
 */

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ReviewsScreen } from "@/screens/ReviewsScreen";
import { useUi } from "@/store/ui";
import { fakeServer, renderWithQuery, stubWebSocket } from "./render";

const ROUND = {
  review: "rev_1",
  at: "2026-08-09T12:00:00Z",
  status: "done",
  notes: [
    { path: "js/game.js", line: 42, note: "the collision check is inverted" },
    { note: "the whole thing feels sluggish" },
  ],
  before: "aaa1111", after: "bbb2222",
  files: ["js/game.js"],
  commits: [
    { sha: "c0ffee1234", short: "c0ffee12", subject: "frontend: fix collision test",
      when: "2 hours ago", who: "trance" },
  ],
};

beforeEach(() => {
  stubWebSocket();
  useUi.setState({ sessionId: "s1", screen: "reviews" });
});
afterEach(() => vi.unstubAllGlobals());

describe("the reviews page", () => {
  it("shows what was asked for and what came of it", async () => {
    fakeServer({ "/api/sessions/s1/reviews": { reviews: [ROUND] } });
    renderWithQuery(<ReviewsScreen />);

    expect(await screen.findByText(/the collision check is inverted/)).toBeInTheDocument();
    expect(screen.getByText("js/game.js:42 —")).toBeInTheDocument();
    // A note with no path is about the project, and says so rather than
    // looking like a note whose file went missing.
    expect(screen.getByText(/the project as a whole/)).toBeInTheDocument();
    expect(screen.getByText("frontend: fix collision test")).toBeInTheDocument();
  });

  it("fetches a patch only when its commit is opened", async () => {
    const user = userEvent.setup();
    const server = fakeServer({
      "/api/sessions/s1/reviews": { reviews: [ROUND] },
      "/api/sessions/s1/commit/c0ffee1234": {
        sha: "c0ffee1234", short: "c0ffee12", subject: "frontend: fix collision test",
        when: "2 hours ago", who: "trance", stat: " 1 file changed, 2 insertions(+)",
        diff: "@@ -1 +1 @@\n-if (a < b)\n+if (a > b)", clipped: false,
      },
    });

    renderWithQuery(<ReviewsScreen />);
    await screen.findByText("frontend: fix collision test");
    // A review that touched thirty files is thirty patches nobody reads at once.
    expect(server.to("/api/sessions/s1/commit/c0ffee1234")).toHaveLength(0);

    await user.click(screen.getByText("frontend: fix collision test"));
    expect(await screen.findByText("+if (a > b)")).toBeInTheDocument();
    expect(screen.getByText(/1 file changed/)).toBeInTheDocument();
  });

  it("says nothing was committed rather than showing an empty section", async () => {
    fakeServer({
      "/api/sessions/s1/reviews": {
        reviews: [{ ...ROUND, commits: [], files: [], status: "done" }],
      },
    });
    renderWithQuery(<ReviewsScreen />);
    expect(await screen.findByText(/Nothing was committed for this review/))
      .toBeInTheDocument();
  });

  it("distinguishes a review still running from one that changed nothing", async () => {
    fakeServer({
      "/api/sessions/s1/reviews": {
        reviews: [{ ...ROUND, commits: [], status: "running" }],
      },
    });
    renderWithQuery(<ReviewsScreen />);
    expect(await screen.findByText(/the step has not finished/)).toBeInTheDocument();
  });

  it("explains how to make one when there are none", async () => {
    fakeServer({ "/api/sessions/s1/reviews": { reviews: [] } });
    renderWithQuery(<ReviewsScreen />);
    expect(await screen.findByText(/No reviews sent yet/)).toBeInTheDocument();
    expect(screen.getByText(/comment on a line/)).toBeInTheDocument();
  });

  it("opens the newest and folds the rest", async () => {
    fakeServer({
      "/api/sessions/s1/reviews": {
        reviews: [
          { ...ROUND, review: "rev_2", notes: [{ note: "newest round" }] },
          { ...ROUND, review: "rev_1", notes: [{ note: "older round" }] },
        ],
      },
    });
    renderWithQuery(<ReviewsScreen />);
    // The one you just sent is the one you came to check.
    expect(await screen.findByText(/newest round/)).toBeInTheDocument();
    expect(screen.queryByText(/older round/)).not.toBeInTheDocument();
  });
});
