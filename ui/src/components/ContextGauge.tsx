/** How full the model's window is for the call it is making.
 *
 * Shown against the *full* window — the number on the model's box — because
 * that is the number people recognize ("46.6k of what?" was the confusion a
 * budget-based gauge caused). The runner still compacts and trims against the
 * input budget (window less the reply room), so that boundary is drawn on the
 * bar as a notch: the fill meeting the notch is the honest picture of "about
 * to compact" without making the denominator a number nobody configured.
 */

import { cn } from "@/lib/cn";
import { tokens } from "@/lib/format";
import type { ContextUsage } from "@/api/types";

export function ContextGauge({ context }: { context: ContextUsage }) {
  const window = Math.max(1, context.window);
  const budget = Math.max(1, Math.min(context.budget || window, window));
  const share = Math.min(1, context.tokens / window);
  const percent = Math.round(share * 100);
  const notch = Math.round((budget / window) * 100);

  // Warm as the *budget* nears, not the window: compaction and trimming
  // happen at the notch, and the gauge must not look calm while they do.
  const ofBudget = Math.round((context.tokens / budget) * 100);
  const tone = ofBudget >= 90 ? "err" : ofBudget >= 70 ? "warn" : "accent";

  return (
    <span
      className="flex items-center gap-1.5 text-[11px] text-muted"
      title={[
        `${context.tokens.toLocaleString()} of the ${window.toLocaleString()}-token window`,
        `${context.reserved.toLocaleString()} reserved for the reply — old rounds compact past ${budget.toLocaleString()} (the notch)`,
        context.estimated ? "estimated — this endpoint reports no usage" : "reported by the model",
      ].join("\n")}
    >
      <span className="relative h-1.5 w-16 overflow-hidden rounded-full bg-line">
        <span
          className={cn("absolute inset-y-0 left-0 rounded-full transition-all",
                        tone === "err" ? "bg-err" : tone === "warn" ? "bg-warn" : "bg-accent")}
          style={{ width: `${Math.max(2, percent)}%` }}
        />
        {notch < 100 && (
          <span className="absolute inset-y-0 w-px bg-fg/50"
                style={{ left: `${notch}%` }} />
        )}
      </span>
      <span className={cn("tabular-nums",
                          tone === "err" && "text-err", tone === "warn" && "text-warn")}>
        {percent}%
      </span>
      <span className="tabular-nums">
        {tokens(context.tokens)}/{tokens(window)}
        {context.estimated && "~"}
      </span>
    </span>
  );
}
