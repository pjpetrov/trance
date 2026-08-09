/** Seeing what the model thought.
 *
 * /events strips the reasoning and the prompt — they are most of an event's
 * weight, and a step of a long run is 13MB with three quarters of it prompts.
 * So the console shows the slim version and asks for the rest when a line is
 * opened, which is the only time anyone reads it. Before this, expanding a
 * model call showed nothing at all.
 */

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { EventLine } from "@/components/EventLine";
import { fakeServer, renderWithQuery, stubWebSocket } from "./render";
import type { TranceEvent } from "@/api/types";

const slim = (over = {}): TranceEvent => ({
  id: "ev1", type: "model_call", session_id: "s1", step_id: "st1",
  ts: "2026-08-09T10:00:00Z", agent: "frontend",
  payload: {
    round: 2, model: "qwen", preset: "Qwen", response_text: "I wrote the file.",
    finish_reason: "stop", usage: { prompt_tokens: 17_700, completion_tokens: 400 },
    // Exactly what the list endpoint leaves behind.
    _omitted: { reasoning: 4_200, messages: 180_000 },
    ...over,
  },
});

beforeEach(() => stubWebSocket());
afterEach(() => vi.unstubAllGlobals());

describe("a model call", () => {
  it("fetches the reasoning only when the line is opened", async () => {
    const user = userEvent.setup();
    const server = fakeServer({
      "/api/sessions/s1/events/ev1": {
        ...slim(),
        payload: { ...slim().payload, reasoning: "first I considered the maze",
                   messages: [{ role: "user", content: "build it" }] },
      },
    });

    renderWithQuery(<EventLine event={slim()} sessionId="s1" />);
    // Nothing asked for yet: this is the request that used to be a 13MB load.
    expect(server.calls).toHaveLength(0);

    await user.click(screen.getByText(/I wrote the file/));
    expect(await screen.findByText(/first I considered the maze/)).toBeInTheDocument();
    expect(screen.getByText(/the full context it was sent \(1 messages\)/))
      .toBeInTheDocument();
  });

  it("does not fetch when there was nothing to strip", async () => {
    const user = userEvent.setup();
    const server = fakeServer({});
    const bare = slim({ _omitted: undefined });

    renderWithQuery(<EventLine event={bare} sessionId="s1" />);
    await user.click(screen.getByText(/I wrote the file/));

    expect(server.calls).toHaveLength(0);
  });

  it("explains a reply that hit the limit before saying anything", async () => {
    const user = userEvent.setup();
    fakeServer({});
    const starved = slim({
      response_text: "", finish_reason: "length", _omitted: undefined,
    });

    renderWithQuery(<EventLine event={starved} sessionId="s1" />);
    await user.click(screen.getByText(/thinking/));

    // The measured failure: max_tokens caps generated tokens, reasoning is
    // generated tokens, so a long think can leave no room for an answer.
    expect(await screen.findByText(/whole budget went to reasoning/))
      .toBeInTheDocument();
  });
});
