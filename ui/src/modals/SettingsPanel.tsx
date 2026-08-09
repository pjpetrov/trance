import { useConfig, useSettings } from "@/api/queries";
import { useSettingsMutations } from "@/api/mutations";
import { Checkbox, Empty } from "@/components/ui/primitives";
import { useUi } from "@/store/ui";
import { toast } from "@/components/Toaster";

export function SettingsPanel() {
  const sessionId = useUi((state) => state.sessionId) ?? "";
  const config = useConfig();
  const settings = useSettings(sessionId);
  const { planning } = useSettingsMutations(sessionId);
  const data = config.data;
  if (!data || !settings.data) return <Empty title="Loading…" />;

  const save = (body: Record<string, unknown>) =>
    planning.mutateAsync(body).catch((error) => toast.err(String(error)));

  return (
    <div className="space-y-5 p-5">
      <section className="space-y-1">
        <h3 className="text-sm font-medium">This project</h3>
        <p className="text-xs leading-relaxed text-muted">
          Its agents, loops, allowlists and these settings live in the project's own
          <code> .trance/</code>, so copying that folder copies the way it is built.
          Models stay on this machine: they carry API keys, and a folder you share is
          the last place for one.
          {settings.data.migrated && (
            <> This project has just taken a copy of your workspace setup; changing it
            here changes it for this project only.</>
          )}
        </p>
      </section>

      <section className="space-y-1">
        <h3 className="text-sm font-medium">Git</h3>
        <p className="text-xs leading-relaxed text-muted">
          Each step runs between two commits, so <code>git log</code> is the record of what
          every agent did — and a step that goes wrong has something to go back to.
        </p>
        <Checkbox
          label="Commit the project after every step"
          checked={settings.data.git_commits}
          onChange={(event) => save({ git_commits: event.target.checked })}
        />
        <Checkbox
          label="Create a repository when the project is not one"
          checked={settings.data.git_auto_init}
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
