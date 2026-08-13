/** One line of the console, and the body it opens onto.
 *
 * The old version was a 300-line `switch` inside the websocket handler, which
 * meant the live console and the step history rendered the same event with two
 * different pieces of code — and drifted. This is the only renderer; both use
 * it, so what you watch happen and what you come back to debug are the same
 * thing by construction.
 *
 * `detail.kind` is a discriminated union in types.ts, so a new kind is a
 * compile error here rather than a line that silently renders as nothing.
 */

import { useEffect, useState, type ReactNode } from "react";
import { useFullEvent } from "@/api/queries";
import { useCommandMutations } from "@/api/mutations";
import { useUi } from "@/store/ui";
import { toast } from "@/components/Toaster";
import { cn } from "@/lib/cn";
import { clip, shortModel, timeOf, tokens } from "@/lib/format";
import { api } from "@/api/client";
import type { ImageDiff, PageErrors, ToolDetail, TranceEvent } from "@/api/types";
import { Badge, Code, type Tone } from "@/components/ui/primitives";

export function EventLine(
  { event, sessionId, defaultOpen, live }:
  { event: TranceEvent; sessionId: string; defaultOpen?: boolean; live?: boolean },
) {
  const rendered = describe(event, sessionId, Boolean(live));
  const [open, setOpen] = useState(Boolean(defaultOpen ?? rendered.open));
  if (!rendered.show) return null;
  const Row = rendered.body ? "button" : "div";

  return (
    <div
      className={cn(
        "rounded-[--radius] border border-transparent transition-colors",
        open && "border-line bg-panel-2",
        rendered.failed && "border-err/40",
      )}
    >
      {/* A row that opens onto something is a button; one that does not is a
          div, because a line with its own control — cancelling the command it
          is reporting — cannot nest a button inside a button. */}
      <Row
        {...(rendered.body ? { onClick: () => setOpen(!open) } : {})}
        className={cn(
          "flex w-full items-baseline gap-2 px-2 py-1 text-left",
          rendered.body && "cursor-pointer hover:bg-panel-2/60",
          !rendered.body && "cursor-default",
          rendered.dim && !open && "opacity-70",
        )}
      >
        <span className="font-code shrink-0 text-[11px] text-muted/70">
          {timeOf(event.ts)}
        </span>
        <span className={cn("w-4 shrink-0 text-center text-xs", rendered.iconTone)}>
          {rendered.icon}
        </span>
        <span className="font-code min-w-0 flex-1 truncate text-xs">{rendered.label}</span>
        {event.agent && (
          <span className="shrink-0 text-[11px] text-muted">{event.agent}</span>
        )}
      </Row>

      {open && rendered.body && <div className="space-y-2 px-2 pb-2">{rendered.body}</div>}
    </div>
  );
}

/** A command that is still running, and the way to stop it.
 *
 * Only its ending was ever drawn, so a command that hung showed nothing at all
 * until the 180s timeout killed it — three minutes of a console that looked
 * idle while the step was blocked on one line.
 */
function RunningCommand({ event }: { event: TranceEvent }) {
  const sessionId = useUi((state) => state.sessionId) ?? "";
  const { cancel } = useCommandMutations(sessionId);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  const payload = event.payload ?? {};
  const seconds = Math.max(0, Math.round((now - new Date(event.ts).getTime()) / 1000));
  return (
    <span className="flex items-baseline gap-2">
      <span className="min-w-0 flex-1 truncate">{String(payload.command ?? "")}</span>
      <span className="shrink-0 tabular-nums text-warn">running {seconds}s</span>
      <button
        className="shrink-0 rounded-[--radius] border border-err/50 px-1.5 text-[11px]
                   text-err hover:bg-err/10 disabled:opacity-50"
        disabled={cancel.isPending}
        onClick={() => cancel.mutateAsync(String(payload.command_id ?? ""))
          .then((result) => toast.ok(result.cancelled
            ? "Command cancelled. The agent is told you stopped it."
            : "That command had already finished."))
          .catch((error) => toast.err(String(error)))}
      >cancel</button>
    </span>
  );
}

/** The call that has not come back yet, counting while it does not. */
function WaitingLabel({ event }: { event: TranceEvent }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  const payload = event.payload ?? {};
  const seconds = Math.max(0, Math.round((now - new Date(event.ts).getTime()) / 1000));
  return (
    <span className="text-accent">
      waiting for {shortModel(payload.model, payload.preset)}
      {payload.thinking === false && <span className="text-warn/80"> · no thinking</span>}
      <span className="ml-2 tabular-nums text-muted">{seconds}s</span>
    </span>
  );
}

interface Rendered {
  show: boolean;
  icon: string;
  iconTone?: string;
  label: ReactNode;
  body?: ReactNode;
  open?: boolean;
  failed?: boolean;
  /** Reads and graph hits: worth keeping, not worth looking at. */
  dim?: boolean;
}

const HIDDEN: Rendered = { show: false, icon: "", label: "" };

function describe(event: TranceEvent, sessionId: string, live: boolean): Rendered {
  const payload = event.payload ?? {};

  if (event.type === "model_call") {
    const wants = (payload.tool_calls ?? []).map((call) => call.name).join(", ");
    return {
      show: true,
      icon: "◐",
      iconTone: "text-purple",
      label: (
        <span>
          {/* The word was hard-coded, which made every call look like a
              thinking one — including the ones sent with thinking off to
              recover a round it had spent entirely on reasoning. */}
          {payload.thinking === false
            ? <span className="text-warn/80">no thinking</span>
            : <span className="text-muted">thinking</span>}{" "}
          {wants
            ? <>→ <span className="text-accent">{wants}</span></>
            : <span className="text-fg/80">{clip(payload.response_text, 90)}</span>}
          <span className="ml-2 text-muted">
            {shortModel(payload.model, payload.preset)}
          </span>
        </span>
      ),
      dim: true,
      body: <ModelCallBody event={event} sessionId={sessionId} />,
    };
  }

  if (event.type === "delegated") {
    return {
      show: true,
      icon: "⇢",
      iconTone: "text-purple",
      label: <span className="text-fg/80">{String(payload.message ?? "")}</span>,
      // The same inspector a model call gets: the run answers nothing for
      // minutes to an hour, and the prompt it was launched with is the one
      // thing there is to examine in the meantime.
      body: <ModelCallBody event={event} sessionId={sessionId} />,
    };
  }

  // One per round, all the same sentence, so the finished ones are noise
  // between the lines that carry the content. The newest one is not: it is the
  // call that has not come back, and without it the console shows nothing at
  // all while a local model spends two minutes on one generation — which is
  // indistinguishable from stuck. So the last one stays, and counts.
  // Drawn only while it is still going: once it ends, its tool_call line says
  // what it did, with the output and the exit code.
  if (event.type === "command_started") {
    if (!live) return HIDDEN;
    return {
      show: true, icon: "$", iconTone: "text-warn",
      label: <RunningCommand event={event} />,
    };
  }

  if (event.type === "model_waiting") {
    if (!live) return HIDDEN;
    return {
      show: true, icon: "◌", iconTone: "text-accent",
      label: <WaitingLabel event={event} />,
    };
  }

  if (event.type !== "tool_call") {
    const message = String(payload.message ?? payload.reason ?? payload.summary ?? "");
    if (!message) return HIDDEN;
    const bad = /fail|error|halt|stop/.test(event.type);
    return {
      show: true,
      icon: bad ? "✕" : "▸",
      iconTone: bad ? "text-err" : "text-muted",
      failed: bad,
      label: <span className="text-fg/80">{message}</span>,
    };
  }

  const detail = payload.detail;
  if (!detail) {
    // ok === false with no detail is a refusal: the tool never ran.
    if (payload.ok === false) {
      return {
        show: true, icon: "✕", iconTone: "text-err", failed: true, open: true,
        label: <span>{payload.name} refused</span>,
        body: <Code>{payload.result}</Code>,
      };
    }
    return HIDDEN;
  }

  return renderDetail(detail, payload.result ?? "", sessionId, payload.name ?? "");
}

function renderDetail(
  detail: ToolDetail, result: string, sessionId: string, toolName: string,
): Rendered {
  switch (detail.kind) {
    case "write":
      return {
        show: true, icon: detail.created ? "✚" : "✎", iconTone: "text-ok", open: true,
        label: (
          <span>
            {detail.appended ? "append " : detail.created ? "create " : "edit "}
            <span className="text-accent">{detail.path}</span>
            <span className="ml-2 text-ok">+{detail.added}</span>
            <span className="ml-1 text-err">−{detail.removed}</span>
          </span>
        ),
        body: <Diff diff={detail.diff} />,
      };

    case "command": {
      const failed = detail.exit_code !== 0 || detail.timed_out || detail.cancelled;
      return {
        show: true, icon: "$", iconTone: failed ? "text-err" : "text-warn",
        failed, open: failed,
        label: (
          <span>
            {detail.command}
            <span className={cn("ml-2", failed ? "text-err" : "text-ok")}>
              {detail.timed_out ? `timed out after ${detail.seconds}s`
                : detail.cancelled ? "cancelled"
                : `exit ${detail.exit_code}`}
            </span>
          </span>
        ),
        body: <Code>{detail.output || "(no output)"}</Code>,
      };
    }

    case "graph":
      return {
        show: true, icon: "⌕", iconTone: "text-muted", dim: detail.hit, open: !detail.hit,
        label: (
          <span>
            {toolName} <span className="text-accent">{clip(detail.query, 48)}</span>
            {!detail.hit && <span className="ml-2 text-muted">no match</span>}
          </span>
        ),
        body: <Code>{result}</Code>,
      };

    case "read":
      return {
        show: true, icon: "◇", iconTone: "text-muted", dim: true,
        label: (
          <span>
            {toolName} <span className="text-accent">{detail.path}</span>
            {detail.outline && (
              <span className="ml-2 text-muted">outline · {detail.symbols} symbols</span>
            )}
            {detail.deduped && <span className="ml-2 text-muted">already in context</span>}
          </span>
        ),
        body: <Code>{result}</Code>,
      };

    case "memory":
      return {
        show: true, icon: "🧠", iconTone: "text-ok",
        label: (
          <span>
            {detail.stored ? "remembered " : "already known "}
            <span className="text-accent">{clip(detail.note, 80)}</span>
          </span>
        ),
      };

    // ------------------------------------------------------- the browser

    case "page": {
      const bad = detail.blank === true || detail.errors.total > 0 || detail.needs_build;
      return {
        show: true, icon: "◱", iconTone: bad ? "text-err" : "text-purple",
        failed: bad, open: bad,
        label: (
          <span>
            opened <span className="text-accent">{detail.page || detail.url}</span>
            <span className="ml-2 text-muted">
              {detail.canvas ? `canvas ${detail.size}` : "no canvas"}
            </span>
            {detail.blank === true && <span className="ml-2 text-err">BLANK</span>}
          </span>
        ),
        body: (
          <>
            {detail.needs_build && (
              <p className="text-xs text-err">
                Served statically, but this project needs a build — what you see may be
                the failure rather than the app.
              </p>
            )}
            <PageErrorList errors={detail.errors} />
            <Code>{result}</Code>
          </>
        ),
      };
    }

    case "canvas": {
      const bad = detail.blank === true || detail.moving === false;
      return {
        show: true, icon: "◱", iconTone: bad ? "text-err" : "text-purple",
        failed: bad, open: bad,
        label: (
          <span>
            canvas <span className="text-accent">{detail.size || "none"}</span>
            <span className={cn("ml-2", detail.blank === true ? "text-err" : "text-muted")}>
              {detail.blank === true ? "BLANK" : detail.blank === false ? "painted" : "unreadable"}
            </span>
            <span className={cn("ml-2", detail.moving === false ? "text-err" : "text-ok")}>
              {detail.moving === false ? "FROZEN" : detail.moving ? "moving" : ""}
            </span>
          </span>
        ),
        body: <><PageErrorList errors={detail.errors} /><Code>{result}</Code></>,
      };
    }

    case "key": {
      const lost = detail.delivered === false;
      return {
        show: true, icon: "⌨", iconTone: lost ? "text-err" : "text-purple",
        failed: lost, open: lost || detail.changed === false,
        label: (
          <span>
            pressed <span className="text-accent">{detail.key}</span>
            {detail.times > 1 && <span className="text-muted"> ×{detail.times}</span>}
            <span className="ml-2 text-muted">after {detail.frames} frames</span>
            <span className={cn("ml-2",
              lost || detail.changed === false ? "text-err" : "text-ok")}>
              {lost ? "never reached the page"
                : detail.changed === true ? "the screen changed"
                : detail.changed === false ? "nothing changed" : ""}
            </span>
          </span>
        ),
        body: <ShotPair detail={detail} sessionId={sessionId} result={result} />,
      };
    }

    case "click": {
      const missed = detail.delivered === false;
      return {
        show: true, icon: "🖰", iconTone: missed ? "text-err" : "text-purple",
        failed: missed, open: missed || detail.changed === false,
        label: (
          <span>
            clicked <span className="text-accent">
              {detail.label || detail.text || `(${detail.x}, ${detail.y})`}
            </span>
            {detail.changed === false && <span className="text-warn"> — no visible effect</span>}
          </span>
        ),
        body: <ShotPair detail={detail} sessionId={sessionId} result={result} />,
      };
    }

    case "wait":
      return {
        show: true, icon: "⏱", iconTone: detail.stalled ? "text-err" : "text-purple",
        failed: detail.stalled, open: detail.stalled || detail.changed === false,
        label: (
          <span>
            waited {detail.frames} frames
            <span className={cn("ml-2",
              detail.stalled || detail.changed === false ? "text-err" : "text-ok")}>
              {detail.stalled ? `stopped short of ${detail.asked_frames} — page blocked`
                : detail.changed === false ? "nothing moved" : "the screen moved"}
            </span>
          </span>
        ),
        body: <ShotPair detail={detail} sessionId={sessionId} result={result} />,
      };

    case "screenshot":
      return {
        show: true, icon: "◉", iconTone: "text-purple", failed: Boolean(detail.error),
        open: true,
        label: (
          <span>
            looked <span className="text-accent">{clip(detail.question, 70)}</span>
            {detail.usage?.total_tokens && (
              <span className="ml-2 text-muted">{tokens(detail.usage.total_tokens)} tok</span>
            )}
          </span>
        ),
        body: <Screenshot detail={detail} sessionId={sessionId} result={result} />,
      };

    case "film":
      return {
        show: true, icon: "▶", iconTone: "text-purple", failed: Boolean(detail.error),
        open: true,
        label: (
          <span>
            watched <span className="text-accent">{clip(detail.question, 70)}</span>
            <span className="ml-2 text-muted">
              {detail.shots.length} frames
              {detail.usage?.total_tokens
                ? ` · ${tokens(detail.usage.total_tokens)} tok` : ""}
            </span>
          </span>
        ),
        body: <Film detail={detail} sessionId={sessionId} result={result} />,
      };

    case "truncated":
      return {
        show: true, icon: "✕", iconTone: "text-err", failed: true, open: true,
        label: <span>tool call cut off at the {detail.limit}-token output limit</span>,
        body: <Code>{result}</Code>,
      };

    case "edit_miss":
    case "edit_ambiguous":
      return {
        show: true, icon: "✎", iconTone: "text-err", failed: true, open: true,
        label: (
          <span>
            edit did not apply{" "}
            <span className="text-accent">{detail.path ?? detail.symbol}</span>
            <span className="ml-2 text-muted">
              {detail.count ? `${detail.count} matches` : "no match"}
            </span>
          </span>
        ),
        body: <Code>{result}</Code>,
      };

    default: {
      const label = "kind" in detail ? String(detail.kind).replace(/_/g, " ") : toolName;
      const failed = String(label).includes("failed");
      return {
        show: true, icon: failed ? "✕" : "▸",
        iconTone: failed ? "text-err" : "text-muted",
        failed, open: failed,
        label: <span>{toolName} — {label}</span>,
        body: <Code>{result}</Code>,
      };
    }
  }
}

/** What a model call did, including the parts /events drops.
 *
 *  The reasoning and the prompt are stripped from the list — they are most of
 *  its weight — so opening a line fetches the event in full. Which is the only
 *  time anyone wants them, and why they were dropped rather than kept.
 */
function ModelCallBody({ event, sessionId }: { event: TranceEvent; sessionId: string }) {
  const payload = event.payload ?? {};
  const omitted = payload._omitted ?? {};
  const wanted = Boolean(omitted.reasoning || omitted.messages);
  const full = useFullEvent(sessionId, wanted ? event.id : null);
  const complete = full.data?.payload ?? payload;

  return (
    <>
      <Stat
        items={[
          ["round", payload.round],
          ["in", payload.usage?.prompt_tokens],
          ["out", payload.usage?.completion_tokens],
          ["ms", payload.duration_ms],
          ["finish", payload.finish_reason],
          // Absent on backends whose thinking we do not set, rather than
          // reported as "on" for a setting nobody chose.
          ...(payload.thinking === undefined
            ? []
            : [["thinking", payload.thinking ? "on" : "off"] as [string, unknown]]),
        ]}
      />

      {full.isLoading && <p className="text-xs text-muted">fetching the full call…</p>}

      {complete.reasoning ? (
        <details open>
          <summary className="cursor-pointer text-xs text-muted">
            thinking ({complete.reasoning.length.toLocaleString()} chars)
          </summary>
          <Code className="mt-1">{complete.reasoning}</Code>
        </details>
      ) : payload.finish_reason === "length" && !payload.response_text ? (
        <p className="text-xs text-warn">
          This reply hit the output limit before it said anything. On a thinking model
          that usually means the whole budget went to reasoning.
        </p>
      ) : null}

      {complete.response_text && <Code>{complete.response_text}</Code>}

      {/* A long run's prompts are hundreds of kilobytes each, so old events
          keep a sentence in place of the value. That sentence arrives in the
          same field, which is why this asks what it is holding rather than
          assuming — assuming it was an array took the whole page down. */}
      {typeof complete.messages === "string" && complete.messages && (
        <p className="text-xs text-muted">{complete.messages}</p>
      )}

      {Array.isArray(complete.messages) && complete.messages.length > 0 && (
        <details>
          <summary className="cursor-pointer text-xs text-muted">
            the full context it was sent ({complete.messages.length} messages)
          </summary>
          <div className="mt-1 space-y-1">
            {complete.messages.map((message, index) => (
              <div key={index}>
                <div className="text-[11px] uppercase tracking-wide text-muted">
                  {message.role}
                </div>
                <Code>{message.content || "(no text — a tool call)"}</Code>
              </div>
            ))}
          </div>
        </details>
      )}
    </>
  );
}

// --------------------------------------------------------------- fragments

function Stat({ items }: { items: [string, unknown][] }) {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted">
      {items.filter(([, value]) => value !== undefined && value !== null).map(([key, value]) => (
        <span key={key}>{key} <b className="text-fg/80">{String(value)}</b></span>
      ))}
    </div>
  );
}

function PageErrorList({ errors }: { errors: PageErrors }) {
  const rows: [string, string[]][] = [
    ["exception", errors.exceptions ?? []],
    ["console", errors.console ?? []],
    ["request failed", errors.failed_requests ?? []],
  ];
  const any = rows.some(([, list]) => list.length);
  if (!any) return <p className="text-xs text-muted">No errors on the page.</p>;
  return (
    <div className="space-y-0.5">
      {rows.flatMap(([label, list]) =>
        [...new Set(list)].map((text) => (
          <div key={`${label}:${text}`} className="font-code text-xs break-words text-err">
            {label}: {text}
          </div>
        )))}
    </div>
  );
}

function Diff({ diff }: { diff: string }) {
  return (
    <pre className="font-code overflow-x-auto rounded-[--radius] bg-bg/60 p-2.5">
      {(diff ?? "").split("\n").map((line, index) => (
        <div
          key={index}
          className={cn(
            line.startsWith("+") && !line.startsWith("+++") && "bg-ok-soft text-ok",
            line.startsWith("-") && !line.startsWith("---") && "bg-err-soft text-err",
            line.startsWith("@@") && "text-purple",
          )}
        >
          {line || " "}
        </div>
      ))}
    </pre>
  );
}

/** Before and after, so "nothing changed" is checkable by eye rather than
 *  taken on trust. That claim has been wrong: a WebGL canvas reads back empty,
 *  and the digest of an empty buffer is a constant. */
function ShotPair(
  { detail, sessionId, result }:
  { detail: Extract<ToolDetail, { kind: "key" | "wait" | "click" }>; sessionId: string; result: string },
) {
  const diff = detail.diff as ImageDiff | null | undefined;
  return (
    <div className="space-y-2">
      {(detail.shot_before || detail.shot_after) && (
        <div className="flex flex-wrap items-start gap-3">
          {([["before", detail.shot_before], ["after", detail.shot_after]] as const).map(
            ([label, shot]) => (
              <figure key={label} className="space-y-1">
                <figcaption className="text-[11px] uppercase tracking-wide text-muted">
                  {label}
                </figcaption>
                {shot
                  ? <Shot sessionId={sessionId} shot={shot} className="max-h-64" />
                  : <div className="text-xs text-muted">not captured</div>}
              </figure>
            ))}
        </div>
      )}
      {diff?.described && (
        <p className={cn("text-xs", diff.identical ? "text-err" : "text-muted")}>
          {diff.described}
        </p>
      )}
      <Code>{result}</Code>
    </div>
  );
}

function Screenshot(
  { detail, sessionId, result }:
  { detail: Extract<ToolDetail, { kind: "screenshot" }>; sessionId: string; result: string },
) {
  const meta = [
    detail.region && `${Math.round(detail.region.width)}×${Math.round(detail.region.height)}`,
    detail.clipped ? "canvas only" : "whole page",
    detail.preset ?? detail.model,
  ].filter(Boolean).join(" · ");

  return (
    <div className="space-y-2">
      {detail.shot && <Shot sessionId={sessionId} shot={detail.shot} className="max-h-[26rem]" />}
      {meta && <p className="text-[11px] text-muted">{meta}</p>}

      <Label>asked</Label>
      <Code>{detail.question}</Code>
      {detail.checks?.length > 0 && (
        <ul className="list-disc space-y-0.5 pl-5 text-xs text-muted">
          {detail.checks.map((check) => <li key={check}>{check}</li>)}
        </ul>
      )}

      <Label>{detail.error ? "no answer" : "answered"}</Label>
      <Code>{detail.error || detail.answer || result}</Code>

      {detail.prompt && (
        <details>
          <summary className="cursor-pointer text-xs text-muted">
            the full prompt it was sent
          </summary>
          <Code className="mt-1">{detail.prompt}</Code>
        </details>
      )}
    </div>
  );
}

function Film(
  { detail, sessionId, result }:
  { detail: Extract<ToolDetail, { kind: "film" }>; sessionId: string; result: string },
) {
  // The frames play as a loop — a flick-book, which is the point of having
  // taken them: motion reads at a glance where eight thumbnails do not.
  const [frame, setFrame] = useState(0);
  const [playing, setPlaying] = useState(true);
  const held = detail.shots.length;

  useEffect(() => {
    if (!playing || held < 2) return;
    // Roughly the capture spacing, floored so a tight burst still reads.
    const ms = Math.max(150, (detail.frames_between / 60) * 1000);
    const timer = setInterval(() => setFrame((at) => (at + 1) % held), ms);
    return () => clearInterval(timer);
  }, [playing, held, detail.frames_between]);

  const shot = detail.shots[frame];

  return (
    <div className="space-y-2">
      {shot && (
        <button onClick={() => setPlaying(!playing)} title={playing ? "Pause" : "Play"}
                className="relative block">
          <Shot sessionId={sessionId} shot={shot} className="max-h-[26rem]" />
          <span className="absolute bottom-1 right-1 rounded-[--radius] bg-black/60
                           px-1.5 py-0.5 text-[11px] text-white">
            {playing ? "❚❚" : "▶"} {frame + 1}/{held}
          </span>
        </button>
      )}
      {!playing && held > 1 && (
        <input
          type="range" min={0} max={held - 1} value={frame}
          onChange={(event) => setFrame(Number(event.target.value))}
          className="w-full"
        />
      )}
      <p className="text-[11px] text-muted">
        {held} frames over {detail.frames} animation frames
        {detail.moving ? "" : " — the screen never changed"}
        {detail.preset || detail.model ? ` · ${detail.preset ?? detail.model}` : ""}
      </p>

      <Label>asked</Label>
      <Code>{detail.question}</Code>
      {detail.checks?.length > 0 && (
        <ul className="list-disc space-y-0.5 pl-5 text-xs text-muted">
          {detail.checks.map((check) => <li key={check}>{check}</li>)}
        </ul>
      )}

      <Label>{detail.error ? "no answer" : "answered"}</Label>
      <Code>{detail.error || detail.answer || result}</Code>
    </div>
  );
}

function Label({ children }: { children: ReactNode }) {
  return (
    <div className="text-[11px] font-semibold uppercase tracking-wide text-muted">
      {children}
    </div>
  );
}

function Shot(
  { sessionId, shot, className }: { sessionId: string; shot: string; className?: string },
) {
  const [gone, setGone] = useState(false);
  if (gone) {
    return <div className="text-xs text-muted">screenshot {shot} is no longer on disk</div>;
  }
  return (
    <img
      src={api.shotUrl(sessionId, shot)}
      alt="what the vision model was shown"
      loading="lazy"
      onError={() => setGone(true)}
      className={cn("rounded-[--radius] border border-line bg-black", className)}
      style={{ imageRendering: "pixelated" }}
    />
  );
}

export function toneForOutcome(outcome: string | undefined): Tone {
  if (outcome === "SUCCESS" || outcome === "PASS") return "ok";
  if (!outcome) return "neutral";
  // Nobody could tell. Red would say the work is wrong, which is not what was
  // found — what was found is that nothing found anything.
  if (outcome === "UNVERIFIED" || outcome === "UNKNOWN") return "warn";
  return "err";
}

export { Badge };
