/** This project, or what new projects start from.
 *
 * Agents, loops and allowlists belong to a project — tuning one for a game
 * must not change it for every other project. That left no way to change what
 * the *next* project starts from, so a prompt improved in four projects had to
 * be improved a fifth time in the fifth. The workspace-wide files are the
 * template; this is the switch between editing a copy and editing the template.
 *
 * Changing the template never reaches back into projects that already exist:
 * they were copied at creation and have moved on since.
 */

import { cn } from "@/lib/cn";

/** The session id that addresses the workspace-wide configuration. Real ids
 *  are `s_…`, so it cannot collide with a project. */
export const DEFAULTS = "defaults";

export type Scope = "project" | "defaults";

export function ScopeSwitch(
  { scope, onChange, what }:
  { scope: Scope; onChange: (scope: Scope) => void; what: string },
) {
  return (
    <div className="flex items-center gap-3">
      <div className="flex rounded-[--radius] border border-line p-0.5 text-xs">
        {([["project", "Current session"],
           ["defaults", "Default"]] as const).map(([id, label]) => (
          <button
            key={id}
            onClick={() => onChange(id)}
            className={cn(
              "rounded-[--radius] px-2 py-1 transition-colors",
              scope === id ? "bg-accent-soft text-fg" : "text-muted hover:text-fg",
            )}
          >{label}</button>
        ))}
      </div>
      <p className="text-xs text-muted">
        {scope === "project"
          ? `The ${what} this session runs with.`
          : `The ${what} every new session is created with. Sessions that already `
            + "exist keep their own copy."}
      </p>
    </div>
  );
}

/** Which id the queries and mutations should use for the chosen scope. */
export function idFor(scope: Scope, sessionId: string | null): string {
  return scope === "defaults" ? DEFAULTS : (sessionId ?? "");
}
