/** The question that has the run stopped.
 *
 * An agent that wants to do something outside its permissions asks, and waits.
 * The asking worked, the waiting worked, and nothing drew the question — so the
 * run simply stopped for five minutes and then carried on refused, with the
 * console showing a step that had gone quiet. Found live: `rm` on a throwaway
 * test script the agent had just been told to clean up after itself.
 *
 * Drawn over whatever screen you are on, because it is not a fact about a
 * screen — it is the run, waiting for you.
 */

import { useApprovals } from "@/api/queries";
import { useApprovalDecision } from "@/api/mutations";
import { useUi } from "@/store/ui";
import { Button } from "@/components/ui/primitives";
import { toast } from "@/components/Toaster";

export function ApprovalPrompt() {
  const sessionId = useUi((state) => state.sessionId);
  const asks = useApprovals(sessionId);
  const decide = useApprovalDecision(sessionId ?? "");

  const waiting = asks.data?.pending ?? [];
  const ask = waiting[0];
  if (!ask) return null;

  const programs = ask.detail?.programs ?? [];
  const answer = (decision: "once" | "always" | "deny") => () =>
    decide.mutateAsync({ id: ask.id, decision })
      .then((result) => toast.ok(
        decision === "deny" ? "Refused. The agent is told, and carries on without it."
          : result.widened ? `Allowed, and ${programs.join(", ")} added to the allowlist.`
          : "Allowed, this once."))
      .catch((error) => toast.err(String(error)));

  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-4 z-50 flex justify-center px-4">
      <div className="pointer-events-auto w-full max-w-2xl rounded-[--radius-lg] border
                      border-warn/50 bg-panel shadow-lg">
        <div className="flex items-start gap-3 border-b border-line px-4 py-3">
          <span className="mt-0.5 text-warn">⚠</span>
          <div className="min-w-0 flex-1">
            <p className="text-sm">
              {ask.message
                ?? `${ask.agent} is asking for something outside its permissions.`}
            </p>
            <code className="font-code mt-1 block break-all text-xs text-accent">
              {ask.subject}
            </code>
            <p className="mt-1 text-[11px] text-muted">
              The run is stopped until you answer. Left alone it is refused, and the
              agent carries on without it.
              {waiting.length > 1 && ` ${waiting.length - 1} more waiting.`}
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2 px-4 py-2">
          <Button variant="primary" size="sm" busy={decide.isPending}
                  onClick={answer("once")}>Allow once</Button>
          {programs.length > 0 && (
            <Button size="sm" busy={decide.isPending} onClick={answer("always")}>
              Always allow {programs.join(", ")}
            </Button>
          )}
          <div className="flex-1" />
          <Button variant="danger" size="sm" busy={decide.isPending}
                  onClick={answer("deny")}>Refuse</Button>
        </div>
      </div>
    </div>
  );
}
