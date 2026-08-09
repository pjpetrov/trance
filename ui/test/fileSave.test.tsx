/** Saving a file writes what was saved into the cache.
 *
 * Regression: the editor drops its draft the moment the save returns, so
 * between that and a refetch landing it fell back to the old cached content —
 * which reads as the save having done nothing at all.
 */

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { keys } from "@/api/queries";
import { useFileMutations } from "@/api/mutations";
import { fakeServer } from "./render";

let client: QueryClient;
const wrapper = ({ children }: { children: ReactNode }) =>
  <QueryClientProvider client={client}>{children}</QueryClientProvider>;

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
});
afterEach(() => vi.unstubAllGlobals());

describe("saving a file", () => {
  it("puts the saved content in the cache rather than waiting for a refetch", async () => {
    fakeServer({
      "/api/sessions/s1/file": { path: "a.js", bytes: 12, committed: true },
    });
    // What the editor was showing before the save.
    client.setQueryData(keys.file("s1", "a.js"),
                        { path: "a.js", content: "old", bytes: 3, lines: 1 });

    const { result } = renderHook(() => useFileMutations("s1"), { wrapper });
    await result.current.write.mutateAsync({ path: "a.js", content: "new\nlonger" });

    await waitFor(() => {
      const held = client.getQueryData(keys.file("s1", "a.js")) as { content: string;
                                                                    lines: number };
      expect(held.content).toBe("new\nlonger");
      // The counts in the header have to move with it, or the file says it is
      // one line long while showing two.
      expect(held.lines).toBe(2);
    });
  });

  it("marks the file list stale, since sizes and counts came from it", async () => {
    fakeServer({ "/api/sessions/s1/file": { path: "a.js", bytes: 3, committed: true } });
    const invalidate = vi.spyOn(client, "invalidateQueries");

    const { result } = renderHook(() => useFileMutations("s1"), { wrapper });
    await result.current.write.mutateAsync({ path: "a.js", content: "x" });

    expect(invalidate).toHaveBeenCalledWith(
      expect.objectContaining({ queryKey: keys.files("s1") }));
  });

  it("drops a deleted file from the cache instead of leaving it readable", async () => {
    fakeServer({ "/api/sessions/s1/file": { deleted: "a.js", committed: true } });
    client.setQueryData(keys.file("s1", "a.js"),
                        { path: "a.js", content: "gone soon", bytes: 9, lines: 1 });

    const { result } = renderHook(() => useFileMutations("s1"), { wrapper });
    await result.current.remove.mutateAsync("a.js");

    await waitFor(() =>
      expect(client.getQueryData(keys.file("s1", "a.js"))).toBeUndefined());
  });
});
