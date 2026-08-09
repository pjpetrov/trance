import { useConfig } from "@/api/queries";
import { useSettingsMutations } from "@/api/mutations";
import { useUi } from "@/store/ui";
import { Button, Checkbox, Empty } from "@/components/ui/primitives";
import { toast } from "@/components/Toaster";

export function SettingsPanel() {
  const config = useConfig();
  const { planning } = useSettingsMutations();
  const openModal = useUi((state) => state.openModal);
  const data = config.data;
  if (!data) return <Empty title="Loading…" />;

  const save = (body: Record<string, unknown>) =>
    planning.mutateAsync(body).catch((error) => toast.err(String(error)));

  return (
    <div className="space-y-5 p-5">
      <section className="space-y-2">
        <h3 className="text-sm font-medium">Orchestrator</h3>
        <p className="text-xs leading-relaxed text-muted">
          The agent you talk to. It designs the team and the order of work, and writes no
          code itself. Its prompt and its model are both on its agent card — this used to
          offer a second model picker that disagreed with the one there.
        </p>
        <div className="flex items-center gap-2">
          <Button size="sm" onClick={() => openModal("agents")}>Open the orchestrator</Button>
          <span className="text-xs text-muted">
            currently {data.orchestrator.model || "unset"}
          </span>
        </div>
      </section>

      <section className="space-y-1">
        <h3 className="text-sm font-medium">Git</h3>
        <p className="text-xs leading-relaxed text-muted">
          Each step runs between two commits, so <code>git log</code> is the record of what
          every agent did — and a step that goes wrong has something to go back to.
        </p>
        <Checkbox
          label="Commit the project after every step"
          checked={data.planning.git_commits}
          onChange={(event) => save({ git_commits: event.target.checked })}
        />
        <Checkbox
          label="Create a repository when the project is not one"
          checked={data.planning.git_auto_init}
          onChange={(event) => save({ git_auto_init: event.target.checked })}
        />
      </section>

      <section className="space-y-2">
        <h3 className="text-sm font-medium">Visual testing</h3>
        <p className="text-xs leading-relaxed text-muted">
          {data.visual.browser
            ? "Chrome found. An agent with the browser toolset can open the app and look at "
              + "it; screenshots go to that agent's own model, which must be able to see "
              + "images."
            : "No Chrome or Chromium on this machine — the browser toolset reports itself "
              + "unavailable and every other toolset works as before."}
        </p>
      </section>
    </div>
  );
}
