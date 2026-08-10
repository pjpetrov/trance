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

import { useLifetimeUsage, useSession, useUsage } from "@/api/queries";
import { useUi } from "@/store/ui";
import { cn } from "@/lib/cn";
import { duration, tokens } from "@/lib/format";
import { Empty, Panel, PanelHeader, Spinner } from "@/components/ui/primitives";
import type { ModelSpend, Usage } from "@/api/types";

export function StatsScreen() {
  const sessionId = useUi((state) => state.sessionId);
  const session = useSession(sessionId);
  const here = useUsage(sessionId);
  const ever = useLifetimeUsage();

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

        <Spend
          title="By model, this session"
          subtitle="every call the agents and the orchestrator made"
          usage={here.data}
          loading={here.isLoading}
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
  { title, subtitle, usage, loading }:
  { title: string; subtitle: string; usage: Usage | undefined; loading: boolean },
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
              {rows.map((row) => <Row key={row.model} row={row} most={most} />)}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

function Row({ row, most }: { row: ModelSpend; most: number }) {
  // The share bar sits behind the name rather than in a column of its own:
  // the question it answers — "which of these is the expensive one" — is about
  // the row, and a separate column of bars reads as a second table.
  const share = Math.round((row.total / most) * 100);
  const perCall = row.calls ? Math.round(row.input_tokens / row.calls) : 0;

  return (
    <tr className="border-t border-line/60">
      <td className="relative px-2 py-1.5">
        <span
          className="absolute inset-y-0.5 left-0 rounded-[--radius] bg-accent/15"
          style={{ width: `${share}%` }}
          aria-hidden
        />
        <span className="relative font-code text-xs">{row.model}</span>
      </td>
      <td className="px-2 py-1.5 text-right tabular-nums text-muted">
        {row.calls.toLocaleString()}
      </td>
      <td className="px-2 py-1.5 text-right tabular-nums">{tokens(row.input_tokens)}</td>
      <td className="px-2 py-1.5 text-right tabular-nums">{tokens(row.output_tokens)}</td>
      <td className={cn("px-2 py-1.5 text-right tabular-nums",
                        perCall > 30_000 ? "text-warn" : "text-muted")}>
        {tokens(perCall)}
      </td>
      <td className="px-2 py-1.5 text-right tabular-nums">{tokens(row.total)}</td>
    </tr>
  );
}
