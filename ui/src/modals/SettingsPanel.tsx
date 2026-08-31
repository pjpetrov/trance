import { useAgents, useConfig, useLoops, useSettings } from "@/api/queries";
import { useSettingsMutations } from "@/api/mutations";
import { Checkbox, Empty } from "@/components/ui/primitives";
import { useUi } from "@/store/ui";
import { toast } from "@/components/Toaster";

function FrameField(
  { label, hint, value, onChange, agentsOnly }:
  { label: string; hint: string; value: string;
    onChange: (name: string) => void; agentsOnly?: boolean },
) {
  const sessionId = useUi((state) => state.sessionId) ?? "";
  const agents = useAgents(sessionId);
  const loops = useLoops(sessionId);
  return (
    <label className="flex items-center gap-2 text-sm" title={hint}>
      <span className="w-24 text-xs text-muted">{label}</span>
      <select
        className="h-7 rounded-[--radius] border border-line bg-bg px-1 text-xs"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">— nothing —</option>
        <optgroup label="agents">
          {(agents.data?.agents ?? [])
            .filter((role) => role.name !== "orchestrator")
            .map((role) => (
              <option key={role.name} value={role.name}>{role.name}</option>
            ))}
        </optgroup>
        {!agentsOnly && (
          <optgroup label="loops">
            {(loops.data ?? []).map((loop) => (
              <option key={loop.name} value={loop.name}>{loop.name}</option>
            ))}
          </optgroup>
        )}
      </select>
    </label>
  );
}

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

      <section className="space-y-1">
        <h3 className="text-sm font-medium">When a step runs out of tries</h3>
        <p className="text-xs leading-relaxed text-muted">
          A halt is a machine that does nothing until you come back to it, and almost
          nothing that stops a step is permanent. Kept on, the step goes round again
          from where it stopped — its own conversation carried over, fresh tries, and
          a note saying what it ran into. A step whose agent or loop no longer exists
          still halts: repeating that is a spin, not a retry.
        </p>
        <Checkbox
          label="Keep trying instead of halting the run"
          checked={settings.data.keep_trying ?? true}
          onChange={(event) => save({ keep_trying: event.target.checked })}
        />
      </section>

      <section className="space-y-2">
        <h3 className="text-sm font-medium">Every plan, always</h3>
        <p className="text-xs leading-relaxed text-muted">
          Enforced on every generated plan, the way the fact check is — what a
          plan must always have is not the model's to forget.
        </p>
        <FrameField
          label="Opens with"
          hint="A planner's step is put first: go over the request against the code, write down the decisions, build nothing."
          value={settings.data.plan_open ?? ""}
          agentsOnly
          onChange={(name) => save({ plan_open: name })}
        />
        <FrameField
          label="Ends with"
          hint="Appended when the plan does not already end with it — a final visual pass over the running app, for instance."
          value={settings.data.plan_close ?? ""}
          onChange={(name) => save({ plan_close: name })}
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
