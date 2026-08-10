/** The verifiers that run after some work, as chips you can take off.
 *
 * Two places ask the same question with the same control: a step ("what should
 * be proved about this task") and an agent ("what should be proved about
 * everything I do"). The engine runs both lists as one chain — the first FAIL
 * sends the work back and the whole chain runs again afterwards, so a fix that
 * breaks an earlier check cannot pass.
 */

import { Select } from "@/components/ui/primitives";
import type { AgentRole } from "@/api/types";

/** The checks that run after a step, as chips you can take off.
 *
 *  One step usually wants more than one kind of proof: that the files exist,
 *  and that nothing which used to pass now fails. The engine has always run a
 *  chain — the first FAIL sends the work back and the whole chain runs again
 *  afterwards, so a fix that breaks an earlier check cannot slip through — but
 *  a step could only ever name one.
 */
export function Checks(
  { checks, verifiers, onChange, label = "Checked by", empty }:
  {
    checks: string[]; verifiers: AgentRole[]; onChange: (checks: string[]) => void;
    label?: string; empty?: string;
  },
) {
  const spare = verifiers.filter((role) => !checks.includes(role.name));

  return (
    <div className="mt-2 flex flex-wrap items-center gap-1.5">
      <span className="text-xs text-muted">{label}</span>
      {checks.map((name) => (
        <span
          key={name}
          className="flex items-center gap-1 rounded-[--radius] bg-accent-soft
                     px-1.5 py-0.5 text-xs"
        >
          {name}
          <button
            title={`Stop running ${name} after this step`}
            className="text-muted hover:text-err"
            onClick={() => onChange(checks.filter((held) => held !== name))}
          >✕</button>
        </span>
      ))}
      {!checks.length && (
        <span className="text-xs text-muted/70">
          {empty ?? "nothing — the agent's own report is taken at face value"}
        </span>
      )}
      {spare.length > 0 && (
        <Select
          className="h-6 w-auto py-0 text-xs"
          value=""
          onChange={(event) => {
            if (event.target.value) onChange([...checks, event.target.value]);
          }}
        >
          <option value="">+ add a check</option>
          {spare.map((role) => (
            <option key={role.name} value={role.name} title={role.description}>
              {role.name}
            </option>
          ))}
        </Select>
      )}
    </div>
  );
}

