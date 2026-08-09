/** The rule the whole data layer rests on.
 *
 * The socket carries what happens *next* and writes it into the query cache, so
 * a live update and a refetch land in the same place. Every bug in the old UI's
 * event handling was a violation of one half of that: history pushed down the
 * socket (23MB before first paint), or the socket writing somewhere the screens
 * did not read from.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, act, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { keys } from "@/api/queries";
import { LIVE_TAIL, useSessionSocket } from "@/hooks/useSessionSocket";
import { event, session } from "./fixtures";

/** A socket the test drives by hand. */
class FakeSocket {
  static live: FakeSocket[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((message: { data: string }) => void) | null = null;
  closed = false;

  constructor(readonly url: string) {
    FakeSocket.live.push(this);
  }

  close() {
    this.closed = true;
    this.onclose?.();
  }

  deliver(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }

  static latest() {
    return FakeSocket.live[FakeSocket.live.length - 1]!;
  }
}

let client: QueryClient;

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  FakeSocket.live = [];
  vi.stubGlobal("WebSocket", FakeSocket as unknown as typeof WebSocket);
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("the session socket", () => {
  it("writes a snapshot straight into the session cache", async () => {
    renderHook(() => useSessionSocket("s1"), { wrapper });
    act(() => { FakeSocket.latest().onopen?.(); });

    act(() => FakeSocket.latest().deliver({
      type: "snapshot", payload: session({ status: "running" }),
    }));

    // The screens read this key. If the socket wrote anywhere else, a running
    // session would render as "ready" until something happened to refetch it.
    expect(client.getQueryData(keys.session("s1"))).toMatchObject({ status: "running" });
  });

  it("appends events and keeps only a bounded tail", () => {
    renderHook(() => useSessionSocket("s1"), { wrapper });
    act(() => { FakeSocket.latest().onopen?.(); });

    act(() => {
      for (let n = 0; n < LIVE_TAIL + 25; n += 1) {
        FakeSocket.latest().deliver(event({ kind: "memory", note: `n${n}`, stored: true }));
      }
    });

    const held = client.getQueryData(keys.events("s1")) as unknown[];
    // An hour-long run is tens of thousands of events. Holding them all is how
    // the tab stops responding; the console shows a window, not an archive.
    expect(held).toHaveLength(LIVE_TAIL);
  });

  it("marks a step's own history stale when that step does something", () => {
    const invalidate = vi.spyOn(client, "invalidateQueries");
    renderHook(() => useSessionSocket("s1"), { wrapper });
    act(() => { FakeSocket.latest().onopen?.(); });

    act(() => FakeSocket.latest().deliver(
      event({ kind: "memory", note: "x", stored: true }, { step_id: "st7" })));

    expect(invalidate).toHaveBeenCalledWith(
      expect.objectContaining({ queryKey: keys.stepEvents("s1", "st7") }));
  });

  it("refetches the console tail on reconnect rather than duplicating it", async () => {
    vi.useFakeTimers();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    renderHook(() => useSessionSocket("s1"), { wrapper });

    act(() => { FakeSocket.latest().onopen?.(); });
    invalidate.mockClear();

    // The server was restarted — which this user does constantly and on purpose.
    act(() => { FakeSocket.latest().onclose?.(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(600); });
    act(() => { FakeSocket.latest().onopen?.(); });

    // Anything that happened while disconnected is not on the socket, so the
    // tail is refetched. It must be an invalidate, never a blind append: the
    // old UI re-pushed history on reconnect and doubled every console line.
    expect(invalidate).toHaveBeenCalledWith(
      expect.objectContaining({ queryKey: keys.events("s1") }));
  });

  it("does not reconnect to a session the user has left", async () => {
    vi.useFakeTimers();
    const { unmount } = renderHook(() => useSessionSocket("s1"), { wrapper });
    act(() => { FakeSocket.latest().onopen?.(); });

    const opened = FakeSocket.live.length;
    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });

    expect(FakeSocket.live.length).toBe(opened);
  });

  it("survives a frame it cannot parse", () => {
    renderHook(() => useSessionSocket("s1"), { wrapper });
    act(() => { FakeSocket.latest().onopen?.(); });

    expect(() => act(() => {
      FakeSocket.latest().onmessage?.({ data: "not json" });
    })).not.toThrow();
  });

  it("opens nothing when there is no session", async () => {
    renderHook(() => useSessionSocket(null), { wrapper });
    await waitFor(() => expect(FakeSocket.live).toHaveLength(0));
  });
});
