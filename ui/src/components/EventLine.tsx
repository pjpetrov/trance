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

import { useState, type ReactNode } from "react";
import { cn } from "@/lib/cn";
import { clip, shortModel, timeOf, tokens } from "@/lib/format";
import { api } from "@/api/client";
import type { ImageDiff, PageErrors, ToolDetail, TranceEvent } from "@/api/types";
import { Badge, Code, type Tone } from "@/components/ui/primitives";

export function EventLine(
  { event, sessionId, defaultOpen }:
  { event: TranceEvent; sessionId: string; defaultOpen?: boolean },
) {
  const rendered = describe(event, sessionId);
  const [open, setOpen] = useState(Boolean(defaultOpen ?? rendered.open));
  if (!rendered.show) return null;

  return (
    <div
      className={cn(
        "rounded-[--radius] border border-transparent transition-colors",
        open && "border-line bg-panel-2",
        rendered.failed && "border-err/40",
      )}
    >
      <button
        onClick={() => rendered.body && setOpen(!open)}
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
      </button>

      {open && rendered.body && <div className="space-y-2 px-2 pb-2">{rendered.body}</div>}
    </div>
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

function describe(event: TranceEvent, sessionId: string): Rendered {
  const payload = event.payload ?? {};

  if (event.type === "model_call") {
    const wants = (payload.tool_calls ?? []).map((call) => call.name).join(", ");
    return {
      show: true,
      icon: "◐",
      iconTone: "text-purple",
      label: (
        <span>
          <span className="text-muted">thinking</span>{" "}
          {wants
            ? <>→ <span className="text-accent">{wants}</span></>
            : <span className="text-fg/80">{clip(payload.response_text, 90)}</span>}
          <span className="ml-2 text-muted">
            {shortModel(payload.model, payload.preset)}
          </span>
        </span>
      ),
      dim: true,
      body: (
        <>
          <Stat
            items={[
              ["round", payload.round],
              ["in", payload.usage?.prompt_tokens],
              ["out", payload.usage?.completion_tokens],
              ["ms", payload.duration_ms],
              ["finish", payload.finish_reason],
            ]}
          />
          {payload.reasoning && <Code>{payload.reasoning}</Code>}
          {payload.response_text && <Code>{payload.response_text}</Code>}
        </>
      ),
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
  { detail: Extract<ToolDetail, { kind: "key" | "wait" }>; sessionId: string; result: string },
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
  return "err";
}

export { Badge };
