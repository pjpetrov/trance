/** Rendering a component the way the app does, minus the network.
 *
 * The old harness stubbed the DOM and asserted on flattened text, so it could
 * only ever check that a render did not throw. Here the DOM is real (jsdom) and
 * only `fetch` is faked, which means a test can click a button and assert on
 * what the server was actually asked for — the class of bug the stub was blind
 * to.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import type { ReactElement } from "react";
import { vi } from "vitest";

/** One route's canned answer. A function so a test can vary the reply by call. */
export type Route = unknown | ((request: FakeRequest) => unknown);

export interface FakeRequest {
  method: string;
  url: string;
  body: unknown;
}

export interface FakeServer {
  /** Every request the component made, in order. */
  calls: FakeRequest[];
  /** Requests to one path, for asserting on what a click sent. */
  to: (path: string) => FakeRequest[];
}

/** Installs a `fetch` that answers from `routes`, matched longest-prefix first.
 *
 *  An unmatched route is a *failed* test, not an empty answer: a component
 *  quietly rendering nothing because a call 404'd is exactly the failure the
 *  old harness used to pass. */
export function fakeServer(routes: Record<string, Route>): FakeServer {
  const calls: FakeRequest[] = [];
  const paths = Object.keys(routes).sort((a, b) => b.length - a.length);

  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const request: FakeRequest = {
      method: init?.method ?? "GET",
      url,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    };
    calls.push(request);

    const key = paths.find((path) => url.split("?")[0] === path || url.startsWith(path));
    if (key === undefined) {
      return new Response(JSON.stringify({ detail: `no fake route for ${url}` }),
                          { status: 404, headers: { "Content-Type": "application/json" } });
    }
    const route = routes[key]!;
    const answer = typeof route === "function"
      ? (route as (request: FakeRequest) => unknown)(request)
      : route;
    return new Response(JSON.stringify(answer),
                        { status: 200, headers: { "Content-Type": "application/json" } });
  });

  return { calls, to: (path) => calls.filter((call) => call.url.split("?")[0] === path) };
}

export function renderWithQuery(element: ReactElement): RenderResult {
  const client = new QueryClient({
    defaultOptions: {
      // Retries turn one deliberate failure into three and a slow test.
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={client}>{element}</QueryClientProvider>,
  );
}

/** A WebSocket that never connects, so a component under test does not try to
 *  reach a server. Tests that care about live events drive the store directly. */
export function stubWebSocket() {
  class Dead {
    onopen: (() => void) | null = null;
    onclose: (() => void) | null = null;
    onerror: (() => void) | null = null;
    onmessage: ((event: { data: string }) => void) | null = null;
    close() {}
  }
  vi.stubGlobal("WebSocket", Dead as unknown as typeof WebSocket);
}
