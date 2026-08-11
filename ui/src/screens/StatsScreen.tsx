/** What the models have actually been asked to do.
 *
 * Tokens, not currency: trance does not know anybody's price list, and a
 * number invented from a stale table would be believed. What it does know is
 * exact — every model call reports its usage on the bus, so these are counts
 * rather than estimates.
 *
 * Input per call is the number worth watching. It is the context being re-sent
 * on every round, and on a long agentic step it dwarfs everything the model
 * writes: measured on one run here, 43.4M in against 700K out.
 */

import { useEventTail, useLifetimeUsage, useSession, useUsage } from "@/api/queries";
import { useUi } from "@/store/ui";
import { cn } from "@/lib/cn";
import { duration, tokens } from "@/lib/format";
import { Dot, Empty, Panel, PanelHeader, Spinner } from "@/components/ui/primitives";
import type { ModelSpend, Usage } from "@/api/types";

export function StatsScreen() {
  const sessionId = useUi((state) => state.sessionId);
  const session = useSession(sessionId);
  const here = useUsage(sessionId);
  const ever = useLifetimeUsage();
  // Who is answering right now. The tail is socket-fed, so this is instant
  // where the counted numbers above are a few seconds behind: the newest
  // event being a model_waiting is exactly the call that has not come back.
  const tail = useEventTail(sessionId);
  const last = tail.data?.[tail.data.length - 1];
  const active = last?.type === "model_waiting"
    ? { name: String((last.payload as { preset?: string; model?: string }).preset
                     ?? (last.payload as { model?: string }).model ?? ""),
        agent: last.agent ?? "" }
    : null;

  if (!sessionId) return <Empty title="No session selected." />;

  const flow = session.data?.flow.steps ?? [];
  const done = flow.filter((step) => step.status === "done").length;
  const failed = flow.filter((step) => step.status === "failed"
                                    || step.status === "halted").length;

  return (
    <div className="h-full overflow-y-auto p-3">
      <div className="mx-auto max-w-4xl space-y-3">
        <Panel>
          <PanelHeader
            title="This session"
            subtitle={session.data?.name ?? ""}
          />
          <div className="grid grid-cols-2 gap-3 p-4 sm:grid-cols-4">
            <Figure label="tokens" value={tokens(here.data?.total ?? 0)} />
            <Figure label="model calls" value={(here.data?.calls ?? 0).toLocaleString()} />
            <Figure label="working time"
                    value={duration(session.data?.run_seconds ?? 0)} />
            <Figure
              label="steps"
              value={`${done} done`}
              hint={failed ? `${failed} failed · ${flow.length} in the plan`
                           : `${flow.length} in the plan`}
            />
          </div>
        </Panel>

        <Effort agents={session.data?.agent_seconds ?? {}} activeAgent={active?.agent} />

        <Spend
          title="By model, this session"
          subtitle="every call the agents and the orchestrator made"
          usage={here.data}
          loading={here.isLoading}
          active={active?.name}
        />

        <Spend
          title="By model, all time"
          subtitle="every session on this machine, including models you have since deleted"
          usage={ever.data}
          loading={ever.isLoading}
        />

        <p className="px-1 pb-2 text-xs text-muted">
          Counted from what each provider reported, not estimated. No prices:
          trance does not know your price list, and a cost invented from a stale
          table would be believed.
        </p>
      </div>
    </div>
  );
}

/** Who the working time went to. The session clock always counted the whole
 *  run; this is the difference between "7h 53m" and knowing the visual tester
 *  ate five of them. */
function Effort(
  { agents, activeAgent }:
  { agents: Record<string, number>; activeAgent?: string },
) {
  const rows = Object.entries(agents)
    .filter(([, seconds]) => seconds > 0)
    .sort(([, a], [, b]) => b - a);
  if (!rows.length) return null;
  const most = Math.max(...rows.map(([, seconds]) => seconds));
  const total = rows.reduce((sum, [, seconds]) => sum + seconds, 0);

  return (
    <Panel>
      <PanelHeader
        title="By agent, this session"
        subtitle="working time: every attempt, fix and check, charged to whoever ran"
      />
      <div className="overflow-x-auto p-2">
        <table className="w-full text-sm">
          <tbody>
            {rows.map(([name, seconds]) => (
              <tr key={name} className="border-t border-line/60 first:border-t-0">
                <td className="relative px-2 py-1.5">
                  <span
                    className="absolute inset-y-0.5 left-0 rounded-[--radius] bg-accent/15"
                    style={{ width: `${Math.round((seconds / most) * 100)}%` }}
                    aria-hidden
                  />
                  <span className="relative inline-flex items-center gap-1.5 font-code text-xs">
                    {name === activeAgent && (
                      <span title="working right now">
                        <Dot tone="accent" pulse />
                      </span>
                    )}
                    {name}
                  </span>
                </td>
                <td className="px-2 py-1.5 text-right tabular-nums">{duration(seconds)}</td>
                <td className="px-2 py-1.5 text-right tabular-nums text-muted">
                  {Math.round((seconds / total) * 100)}%
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

function Figure(
  { label, value, hint }: { label: string; value: string; hint?: string },
) {
  return (
    <div>
      <div className="text-xs text-muted">{label}</div>
      <div className="text-lg tabular-nums">{value}</div>
      {hint && <div className="text-[11px] text-muted">{hint}</div>}
    </div>
  );
}

function Spend(
  { title, subtitle, usage, loading, active }:
  { title: string; subtitle: string; usage: Usage | undefined; loading: boolean;
    active?: string },
) {
  const rows = usage?.models ?? [];
  const most = Math.max(1, ...rows.map((row) => row.total));

  return (
    <Panel>
      <PanelHeader title={title} subtitle={subtitle} />
      {loading && <Spinner className="m-4 text-muted" />}
      {!loading && !rows.length && (
        <Empty title="Nothing has been asked of a model yet." />
      )}
      {rows.length > 0 && (
        <div className="overflow-x-auto p-2">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-muted">
                <th className="px-2 py-1 font-medium">model</th>
                <th className="px-2 py-1 text-right font-medium">calls</th>
                <th className="px-2 py-1 text-right font-medium">in</th>
                <th className="px-2 py-1 text-right font-medium">out</th>
                <th className="px-2 py-1 text-right font-medium">in / call</th>
                <th className="px-2 py-1 text-right font-medium">total</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <Row key={row.model} row={row} most={most}
                     active={row.model === active} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

function Row(
  { row, most, active }: { row: ModelSpend; most: number; active?: boolean },
) {
  // The share bar sits behind the name rather than in a column of its own:
  // the question it answers — "which of these is the expensive one" — is about
  // the row, and a separate column of bars reads as a second table.
  const share = Math.round((row.total / most) * 100);
  const perCall = row.calls ? Math.round(row.input_tokens / row.calls) : 0;
  const cached = row.input_tokens ? (row.cache_read_tokens ?? 0) / row.input_tokens : 0;

  return (
    <tr className="border-t border-line/60">
      <td className="relative px-2 py-1.5">
        <span
          className="absolute inset-y-0.5 left-0 rounded-[--radius] bg-accent/15"
          style={{ width: `${share}%` }}
          aria-hidden
        />
        <span className="relative inline-flex items-center gap-1.5 font-code text-xs">
          {active && (
            <span title="answering right now">
              <Dot tone="accent" pulse />
            </span>
          )}
          {row.model}
        </span>
      </td>
      <td className="px-2 py-1.5 text-right tabular-nums text-muted">
        {row.calls.toLocaleString()}
      </td>
      <td className="px-2 py-1.5 text-right tabular-nums">
        {tokens(row.input_tokens)}
        {cached >= 0.5 && (
          <span
            className="block text-[10px] text-muted"
            title={`${tokens(row.cache_read_tokens ?? 0)} of the input was cache re-reads, `
              + "billed at roughly a tenth of a fresh token — the same conversation "
              + "read back on every internal turn"}
          >{Math.round(cached * 100)}% cached</span>
        )}
      </td>
      <td className="px-2 py-1.5 text-right tabular-nums">{tokens(row.output_tokens)}</td>
      <td className={cn("px-2 py-1.5 text-right tabular-nums",
                        perCall > 30_000 ? "text-warn" : "text-muted")}>
        {tokens(perCall)}
      </td>
      <td className="px-2 py-1.5 text-right tabular-nums">{tokens(row.total)}</td>
    </tr>
  );
}
