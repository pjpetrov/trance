/** The live run, delivered into the query cache.
 *
 * The socket is a transport, not a second state container. A snapshot writes
 * the session query; an event appends to the events query. So a live update and
 * a refetch land in exactly the same place, and no component has to know which
 * one it is looking at. That equivalence is the thing the old UI never had:
 * there, the socket wrote a global object and then hand-called the repaint
 * functions it remembered about.
 *
 * The other rule the server sets and this respects: the socket carries what
 * happens *next*, never history. History is fetched by whoever wants it, in the
 * slice they want. Pushing the lot is what made a long session 23MB before the
 * first paint.
 */

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { keys } from "@/api/queries";
import type { Session, TranceEvent } from "@/api/types";

/** How many live events to keep in the cache. The console shows a window, not
 *  an archive; an hour-long run is tens of thousands of events. */
export const LIVE_TAIL = 600;

/** Capped exponential backoff. This user restarts their server constantly and
 *  on purpose, so reconnecting has to be quick — but a server that is gone for
 *  the afternoon must not be hammered. */
const MIN_RETRY_MS = 500;
const MAX_RETRY_MS = 10_000;

export type SocketState = "connecting" | "open" | "closed";

type SocketMessage = { type: "snapshot"; payload: Session } | TranceEvent;

export function useSessionSocket(sessionId: string | null): SocketState {
  const client = useQueryClient();
  const [state, setState] = useState<SocketState>("closed");
  // Kept in a ref so a reconnect does not re-run the effect and tear down the
  // socket it just opened.
  const attempts = useRef(0);

  useEffect(() => {
    if (!sessionId) {
      setState("closed");
      return;
    }

    let socket: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let live = true;

    const connect = () => {
      if (!live) return;
      setState("connecting");
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(
        `${scheme}://${location.host}/ws/${encodeURIComponent(sessionId)}`);

      socket.onopen = () => {
        attempts.current = 0;
        setState("open");
        // Anything that happened while we were disconnected is not on the
        // socket — it only carries what comes next. The snapshot fixes the
        // session; this refetches the console's tail so the gap is filled.
        void client.invalidateQueries({ queryKey: keys.events(sessionId) });
      };

      socket.onmessage = (message) => {
        let data: SocketMessage;
        try {
          data = JSON.parse(message.data as string) as SocketMessage;
        } catch {
          return;                       // a frame we cannot read is not a crash
        }

        if ("type" in data && data.type === "snapshot") {
          client.setQueryData(keys.session(sessionId), data.payload);
          return;
        }

        const event = data as TranceEvent;
        client.setQueryData<TranceEvent[]>(keys.events(sessionId), (current) => {
          const held = current ?? [];
          // A model_progress frame reuses one id for the whole generation, so
          // each new frame replaces the last in place — one console line that
          // updates while the model thinks, not a line per second of it.
          const at = held.findIndex((h) => h.id === event.id);
          const next = at >= 0
            ? [...held.slice(0, at), ...held.slice(at + 1), event]
            : [...held, event];
          return next.length > LIVE_TAIL ? next.slice(-LIVE_TAIL) : next;
        });

        // A step's own history is fetched per step and cached; when that step
        // does something new, the cached copy is short by exactly this event —
        // so add it, rather than asking for the whole thing again. Measured on
        // a live step: that history was 3.9MB and took 0.45s to fetch, and
        // invalidating it on every event asked for it faster than it could
        // land. The console froze thirteen minutes behind a spinner while the
        // model answered query after query, and only a page reload — one clean
        // fetch, nothing to cancel it — caught it up.
        if (event.step_id) {
          client.setQueryData<TranceEvent[]>(
            keys.stepEvents(sessionId, event.step_id),
            (current) => {
              // Undefined means nobody has opened that step: seeding it with
              // live events alone would pass off a tail as the whole history.
              if (!current) return current;
              // A fetch that lands after the socket delivered the same event
              // would otherwise show it twice, under one React key — and a
              // model_progress frame arrives repeatedly under one id, each
              // frame superseding the last.
              const at = current.findIndex((held) => held.id === event.id);
              if (at >= 0) {
                const copy = [...current];
                copy[at] = event;
                return copy;
              }
              return [...current, event];
            });
        }
        // A blocked agent is the one thing where a five-second poll is too
        // slow to be the only way you hear about it.
        if (event.type === "approval_requested" || event.type === "approval_resolved") {
          void client.invalidateQueries({ queryKey: keys.approvals(sessionId) });
        }
        if (event.type === "file_written" || event.type === "file_edited") {
          void client.invalidateQueries({ queryKey: keys.files(sessionId) });
        }
        if ((event.payload?.detail as { kind?: string } | undefined)?.kind === "memory") {
          void client.invalidateQueries({ queryKey: keys.memory(sessionId) });
        }
      };

      socket.onclose = () => {
        if (!live) return;
        setState("closed");
        const wait = Math.min(MIN_RETRY_MS * 2 ** attempts.current, MAX_RETRY_MS);
        attempts.current += 1;
        retry = setTimeout(connect, wait);
      };

      socket.onerror = () => socket?.close();
    };

    connect();

    return () => {
      live = false;
      if (retry) clearTimeout(retry);
      // Close cleanly and drop the handlers first: onclose would otherwise
      // schedule a reconnect for a session the user has already left.
      if (socket) {
        socket.onclose = null;
        socket.onerror = null;
        socket.close();
      }
      setState("closed");
    };
  }, [sessionId, client]);

  return state;
}
