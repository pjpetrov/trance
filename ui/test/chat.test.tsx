/** Attaching a screenshot to a message.
 *
 * A picture of the bug is often the whole report. What matters here is that it
 * actually leaves the browser attached to the message, and that a failed send
 * gives back what was typed rather than swallowing it.
 */

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HomeScreen } from "@/screens/HomeScreen";
import { useUi } from "@/store/ui";
import { fakeServer, renderWithQuery, stubWebSocket } from "./render";
import { session } from "./fixtures";

const shot = () =>
  new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], "bug.png", { type: "image/png" });

beforeEach(() => {
  stubWebSocket();
  useUi.setState({ sessionId: "s1", screen: "home" });
});
afterEach(() => vi.unstubAllGlobals());

const routes = (over: Record<string, unknown> = {}) => ({
  "/api/sessions": [session()],
  "/api/sessions/s1": session(),
  ...over,
});

describe("the chat composer", () => {
  it("sends a screenshot along with the message", async () => {
    const user = userEvent.setup();
    const server = fakeServer(routes({ "/api/sessions/s1/chat": { session: session() } }));

    renderWithQuery(<HomeScreen />);
    const box = await screen.findByPlaceholderText(/paste or drop a screenshot/);
    await user.type(box, "the ship does not move");

    // The + opens a normal file input, which is what a test can drive.
    const picker = document.querySelector("input[type=file]") as HTMLInputElement;
    await user.upload(picker, shot());
    expect(await screen.findByAltText("attached")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => {
      const sent = server.to("/api/sessions/s1/chat").at(-1);
      const body = sent?.body as { message: string; images: string[] };
      expect(body.message).toBe("the ship does not move");
      expect(body.images).toHaveLength(1);
      expect(body.images[0]).toMatch(/^data:image\/png;base64,/);
    });
  });

  it("gives back what was typed when the send fails", async () => {
    const user = userEvent.setup();
    fakeServer(routes());          // no /chat route: the call 404s

    renderWithQuery(<HomeScreen />);
    const box = await screen.findByPlaceholderText(/paste or drop a screenshot/);
    await user.type(box, "do not lose this");
    await user.click(screen.getByRole("button", { name: "Send" }));

    // Clearing on submit is right; clearing on failure loses the message.
    await waitFor(() => expect(box).toHaveValue("do not lose this"));
  });

  it("shows screenshots that came back with the conversation", async () => {
    fakeServer(routes({
      "/api/sessions/s1": session({
        chat: [{ role: "user", content: "look at this",
                 images: ["chat/abc123.png"] }],
      }),
    }));

    renderWithQuery(<HomeScreen />);
    const shown = await screen.findByAltText("attached screenshot");
    expect(shown).toHaveAttribute("src", "/api/sessions/s1/shot/chat/abc123.png");
  });

  it("will not send an empty message", async () => {
    const user = userEvent.setup();
    const server = fakeServer(routes({ "/api/sessions/s1/chat": { session: session() } }));

    renderWithQuery(<HomeScreen />);
    await screen.findByPlaceholderText(/paste or drop a screenshot/);
    await user.click(screen.getByRole("button", { name: "Send" }));

    expect(server.to("/api/sessions/s1/chat")).toHaveLength(0);
  });
});
