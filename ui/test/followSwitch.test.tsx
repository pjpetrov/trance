/** Following the run, on purpose.
 *
 * It used to be inferred from where the scrollbar happened to be: within 120px
 * of the end and the console followed, otherwise it did not. So it stopped for
 * reasons nothing showed and nothing could turn back on — "sometimes the auto
 * follow stops working" is exactly what an invisible rule feels like. It is a
 * switch now, and the switch says what is happening.
 */

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RunScreen } from "@/screens/RunScreen";
import { useUi } from "@/store/ui";
import { useFollowHarness } from "@/hooks/useFollowHarness";
import { renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { keys } from "@/api/queries";
import type { ReactNode } from "react";
import { fakeServer, renderWithQuery, stubWebSocket } from "./render";
import { eventsRoute, session, step } from "./fixtures";
import type { Session, StepStatus, TranceEvent } from "@/api/types";

let nth = 0;
const line = (): TranceEvent => ({
  id: `e${(nth += 1)}`, type: "tool_call", session_id: "s1", step_id: "st1",
  ts: "2026-08-10T12:00:00Z", agent: "frontend",
  payload: { name: "read_file", ok: true,
             detail: { kind: "read", path: `src/file${nth}.js`, lines: 3 } },
});

const serve = (events: TranceEvent[]) => fakeServer({
  "/api/sessions/s1": session({
    status: "running",
    flow: { steps: [step({ id: "st1", status: "running" })], cursor: 0 },
  }),
  "/api/sessions/s1/events": eventsRoute(events, events),
});

beforeEach(() => {
  stubWebSocket();
  useUi.setState({ sessionId: "s1", screen: "run", openStep: null, follow: true });
});
afterEach(() => vi.unstubAllGlobals());

describe("the follow switch", () => {
  it("is on by default and says so", async () => {
    serve([line()]);
    renderWithQuery(<RunScreen />);
    expect(await screen.findByRole("button", { name: "following" })).toBeInTheDocument();
  });

  it("turns off and on by hand", async () => {
    const user = userEvent.setup();
    serve([line()]);
    renderWithQuery(<RunScreen />);

    await user.click(await screen.findByRole("button", { name: "following" }));
    expect(useUi.getState().follow).toBe(false);
    expect(screen.getByRole("button", { name: "follow" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "follow" }));
    expect(useUi.getState().follow).toBe(true);
  });

  it("keeps the newest lines in view while it is on, and stops when it is off", async () => {
    // Events arrive by being appended to the cache, which is what the socket
    // does now — so this is the real path, not a re-render.
    const scrolled = vi.spyOn(Element.prototype, "scrollIntoView");
    const first = [line()];
    client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    fakeServer({
      "/api/sessions/s1": session({
        status: "running",
        flow: { steps: [step({ id: "st1", status: "running" })], cursor: 0 },
      }),
      "/api/sessions/s1/events": eventsRoute(first, first),
    });
    render(<QueryClientProvider client={client}><RunScreen /></QueryClientProvider>);
    await screen.findAllByText(/src\/file/);

    const arrives = () => client.setQueryData<TranceEvent[]>(
      keys.stepEvents("s1", "st1"), (held) => [...(held ?? []), line()]);

    scrolled.mockClear();
    arrives();
    await waitFor(() =>
      expect(scrolled).toHaveBeenCalledWith(expect.objectContaining({ block: "end" })));

    useUi.setState({ follow: false });
    scrolled.mockClear();
    arrives();
    await new Promise((done) => setTimeout(done, 40));
    expect(scrolled).not.toHaveBeenCalledWith(expect.objectContaining({ block: "end" }));
    scrolled.mockRestore();
  });

  it("pauses when you scroll up to read something", async () => {
    serve([line(), line(), line()]);
    const { container } = renderWithQuery(<RunScreen />);
    await screen.findAllByText(/src\/file/);

    // Two panels scroll: the step rail and the console. The console is the
    // second, and scrolling the rail must not pause anything.
    const box = [...container.querySelectorAll(".overflow-y-auto")].at(-1)!;
    // A tall console scrolled well away from the end.
    Object.defineProperty(box, "scrollHeight", { value: 2000, configurable: true });
    Object.defineProperty(box, "clientHeight", { value: 500, configurable: true });
    Object.defineProperty(box, "scrollTop", { value: 100, configurable: true });
    fireEvent.scroll(box);

    await waitFor(() => expect(useUi.getState().follow).toBe(false));
  });

  it("does not pause for a scroll that is still at the end", async () => {
    serve([line()]);
    const { container } = renderWithQuery(<RunScreen />);
    await screen.findAllByText(/src\/file/);

    // Two panels scroll: the step rail and the console. The console is the
    // second, and scrolling the rail must not pause anything.
    const box = [...container.querySelectorAll(".overflow-y-auto")].at(-1)!;
    Object.defineProperty(box, "scrollHeight", { value: 600, configurable: true });
    Object.defineProperty(box, "clientHeight", { value: 500, configurable: true });
    Object.defineProperty(box, "scrollTop", { value: 100, configurable: true });
    fireEvent.scroll(box);

    await new Promise((done) => setTimeout(done, 30));
    expect(useUi.getState().follow).toBe(true);
  });
});

// ------------------------------------------------- the step half of following

let client: QueryClient;
const wrap = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={client}>{children}</QueryClientProvider>
);

const flow = (...rows: [string, StepStatus][]): Session => session({
  id: "s1", status: "running",
  flow: { steps: rows.map(([id, status]) => step({ id, status })), cursor: 0 },
});

describe("clicking a step", () => {
  it("pauses following, the way scrolling up does", async () => {
    // Otherwise the next transition takes the step away again for no visible
    // reason. The switch says why nothing is moving.
    const user = userEvent.setup();
    useUi.setState({ follow: true, openStep: null });
    fakeServer({
      "/api/sessions/s1": session({
        status: "running",
        flow: {
          steps: [step({ id: "st1", task: "the first step", status: "done", runs: 1 }),
                  step({ id: "st2", task: "the second step", status: "running" })],
          cursor: 0,
        },
      }),
      "/api/sessions/s1/events": eventsRoute([], []),
    });
    renderWithQuery(<RunScreen />);
    await waitFor(() => expect(useUi.getState().openStep).toBe("st2"));
    expect(useUi.getState().follow).toBe(true);   // arriving is not a click

    await user.click(screen.getByText("the first step"));
    expect(useUi.getState().openStep).toBe("st1");
    expect(useUi.getState().follow).toBe(false);
  });
});

describe("following from step to step", () => {
  beforeEach(() => {
    client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  });

  it("moves to the next step while following", async () => {
    client.setQueryData(keys.session("s1"), flow(["st1", "running"], ["st2", "pending"]));
    renderHook(() => useFollowHarness("s1"), { wrapper: wrap });
    await waitFor(() => expect(useUi.getState().openStep).toBe("st1"));

    client.setQueryData(keys.session("s1"), flow(["st1", "done"], ["st2", "running"]));
    await waitFor(() => expect(useUi.getState().openStep).toBe("st2"));
  });

  it("stays put when following is off", async () => {
    // Moving the step out from under someone who paused the console is the
    // same interruption as scrolling it.
    client.setQueryData(keys.session("s1"), flow(["st1", "running"], ["st2", "pending"]));
    renderHook(() => useFollowHarness("s1"), { wrapper: wrap });
    await waitFor(() => expect(useUi.getState().openStep).toBe("st1"));

    useUi.setState({ follow: false });
    client.setQueryData(keys.session("s1"), flow(["st1", "done"], ["st2", "running"]));
    await new Promise((done) => setTimeout(done, 30));
    expect(useUi.getState().openStep).toBe("st1");
  });
});
