/** How full the model's window is for the call it is making.
 *
 * The number people act on: it is what decides whether to raise
 * `context_window`, and whether a step is about to start dropping tool results
 * to fit. Shown as a share of the *budget* — the window less the room reserved
 * for the answer — because that is what the runner actually trims against, and
 * a gauge that disagreed with the trimmer would be worse than none.
 */

import { cn } from "@/lib/cn";
import { tokens } from "@/lib/format";
import type { ContextUsage } from "@/api/types";

export function ContextGauge({ context }: { context: ContextUsage }) {
  const budget = Math.max(1, context.budget || context.window);
  const share = Math.min(1, context.tokens / budget);
  const percent = Math.round(share * 100);

  // Warm before it is a problem: a step that fills the window mid-way starts
  // silently dropping what it already read.
  const tone = percent >= 90 ? "err" : percent >= 70 ? "warn" : "accent";

  return (
    <span
      className="flex items-center gap-1.5 text-[11px] text-muted"
      title={[
        `${context.tokens.toLocaleString()} of ${budget.toLocaleString()} tokens`,
        `window ${context.window.toLocaleString()}, ${context.reserved.toLocaleString()} reserved for the reply`,
        context.estimated ? "estimated — this endpoint reports no usage" : "reported by the model",
      ].join("\n")}
    >
      <span className="relative h-1.5 w-16 overflow-hidden rounded-full bg-line">
        <span
          className={cn("absolute inset-y-0 left-0 rounded-full transition-all",
                        tone === "err" ? "bg-err" : tone === "warn" ? "bg-warn" : "bg-accent")}
          style={{ width: `${Math.max(2, percent)}%` }}
        />
      </span>
      <span className={cn("tabular-nums",
                          tone === "err" && "text-err", tone === "warn" && "text-warn")}>
        {percent}%
      </span>
      <span className="tabular-nums">
        {tokens(context.tokens)}/{tokens(budget)}
        {context.estimated && "~"}
      </span>
    </span>
  );
}
