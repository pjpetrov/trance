/** Starting a project.
 *
 * It used to ask for a name and an absolute project directory, which was the
 * same path every time with the name on the end. The form asks for the name;
 * the server makes the folder. What is worth testing is that the path it shows
 * is the path that gets made — a preview that disagrees with the server is
 * worse than no preview.
 */

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HomeScreen } from "@/screens/HomeScreen";
import { useUi } from "@/store/ui";
import { fakeServer, renderWithQuery, stubWebSocket } from "./render";
import { session } from "./fixtures";

const WORKSPACE = {
  workspace: "/home/you/trance_workspace", state_dir: "/home/you/trance/runs",
  writable: true, suggested_name: "project",
  suggested_dir: "/home/you/trance_workspace/project",
};

beforeEach(() => {
  stubWebSocket();
  useUi.setState({ sessionId: null, screen: "home" });
});
afterEach(() => vi.unstubAllGlobals());

const open = async (user: ReturnType<typeof userEvent.setup>) => {
  renderWithQuery(<HomeScreen />);
  await user.click(await screen.findByRole("button", { name: "New session" }));
};

describe("creating a session", () => {
  it("asks for a name and nothing else", async () => {
    const user = userEvent.setup();
    const server = fakeServer({
      "/api/sessions": ({ method }: { method: string }) =>
        (method === "POST" ? session({ id: "s2", name: "Chicken Invaders" }) : []),
      "/api/sessions/s2": session({ id: "s2", name: "Chicken Invaders" }),
      "/api/workspace": WORKSPACE,
    });

    await open(user);
    // The absolute path field is gone; nobody types a path any more.
    expect(screen.queryByPlaceholderText(/\/home\/you\/projects/)).not.toBeInTheDocument();

    await user.type(screen.getByPlaceholderText("pacman"), "Chicken Invaders");
    // And it says where that lands, before committing to it.
    expect(screen.getByText(/trance_workspace\/chicken-invaders/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => {
      const made = server.calls.find((call) => call.method === "POST");
      expect(made?.body).toEqual({ name: "Chicken Invaders" });
    });
  });

  it("previews the folder the same way the server names it", async () => {
    const user = userEvent.setup();
    fakeServer({ "/api/sessions": [], "/api/workspace": WORKSPACE });
    await open(user);
    const field = screen.getByPlaceholderText("pacman");

    for (const [typed, folder] of [
      ["Pac Man", "pac-man"],
      ["  Spaced  Out  ", "spaced-out"],
      ["Bug #42: the ship!", "bug-42-the-ship"],
      ["../escape", "escape"],           // a name cannot be a path
      ["...", "project"],                // nor a hidden folder, nor nothing
    ] as const) {
      await user.clear(field);
      await user.type(field, typed);
      expect(screen.getByText(new RegExp(`trance_workspace/${folder} —`)))
        .toBeInTheDocument();
    }
  });

  it("says nothing about a path it has not been told", async () => {
    const user = userEvent.setup();
    fakeServer({ "/api/sessions": [] });          // no /api/workspace answer
    await open(user);
    expect(screen.getByText(/folder is made in the workspace/)).toBeInTheDocument();
  });
});
