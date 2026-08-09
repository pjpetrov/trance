import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect } from "react";
import { useConfig, useSessions } from "@/api/queries";
import { useSessionSocket } from "@/hooks/useSessionSocket";
import { useUi } from "@/store/ui";
import { Shell } from "@/components/Shell";
import { Toaster } from "@/components/Toaster";

/** One client for the app.
 *
 * Retries are off by default: nearly every call here is a command against a
 * local server, and silently retrying "stop the run" or "save this agent" is
 * worse than reporting that it failed. Queries that genuinely benefit from a
 * retry ask for one.
 */
const client = new QueryClient({
  defaultOptions: {
    queries: {
      retry: false,
      refetchOnWindowFocus: false,
      // The websocket is the live channel; polling on top of it would ask for
      // what is already arriving.
      staleTime: 5_000,
    },
    mutations: { retry: false },
  },
});

export function App() {
  return (
    <QueryClientProvider client={client}>
      <Bootstrap />
      <Toaster />
    </QueryClientProvider>
  );
}

function Bootstrap() {
  const sessionId = useUi((state) => state.sessionId);
  const selectSession = useUi((state) => state.selectSession);
  const config = useConfig();
  const sessions = useSessions();

  // Live updates for whichever session is open. One socket, and it writes into
  // the query cache rather than into components.
  const socket = useSessionSocket(sessionId);

  // Land on something rather than an empty screen: the most recent session is
  // almost always the one being worked on.
  useEffect(() => {
    if (!sessionId && sessions.data?.length) {
      selectSession(sessions.data[0]!.id);
    }
  }, [sessionId, sessions.data, selectSession]);

  if (config.isLoading) {
    return (
      <div className="grid h-full place-items-center text-sm text-muted">
        Starting up…
      </div>
    );
  }

  if (config.isError) {
    return (
      <div className="grid h-full place-items-center px-6 text-center">
        <div className="space-y-2">
          <div className="text-sm text-err">Cannot reach the trance server.</div>
          <div className="max-w-md text-xs leading-relaxed text-muted">
            {String(config.error)}. The page is served by it, so this usually means it
            stopped after the tab was opened.
          </div>
        </div>
      </div>
    );
  }

  return <Shell socket={socket} />;
}
