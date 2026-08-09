/** The editors, hosted in one place so only one can be open at a time.
 *
 * Agents, models and loops share a shape — a list on the left, the one you are
 * editing on the right — because they are the same kind of thing: a library of
 * named definitions. The old UI made each of them a different layout and the
 * agents one a wall of stacked forms.
 */

import { useUi } from "@/store/ui";
import { Modal } from "@/components/ui/Modal";
import { AgentsEditor } from "./AgentsEditor";
import { ModelsEditor } from "./ModelsEditor";
import { SettingsPanel } from "./SettingsPanel";
import { LoopsEditor } from "./LoopsEditor";
import { CommandsEditor } from "./CommandsEditor";
import { MemoryPanel } from "./MemoryPanel";

export function Modals() {
  const { modal, openModal } = useUi();
  const close = () => openModal(null);

  return (
    <>
      <Modal
        open={modal === "agents"} onClose={close} wide
        title="Agents & permissions"
        subtitle="Each agent's remit is enforced, not advisory: a write outside it fails."
      >
        <AgentsEditor />
      </Modal>

      <Modal
        open={modal === "models"} onClose={close} wide
        title="Models"
        subtitle="A model carries its own endpoint, so an agent picks one thing."
      >
        <ModelsEditor />
      </Modal>

      <Modal
        open={modal === "loops"} onClose={close} wide
        title="Loops"
        subtitle="A block of agents that runs until the work is right — the verdict decides, not a declaration of success."
      >
        <LoopsEditor />
      </Modal>

      <Modal
        open={modal === "commands"} onClose={close} wide
        title="Command allowlists"
        subtitle="Agents with the commands toolset may run these programs, and nothing else."
      >
        <CommandsEditor />
      </Modal>

      <Modal
        open={modal === "memory"} onClose={close}
        title="Project memory"
        subtitle="The only channel between agents: each starts a step with no memory of its own."
      >
        <MemoryPanel />
      </Modal>

      <Modal open={modal === "settings"} onClose={close} title="Settings">
        <SettingsPanel />
      </Modal>
    </>
  );
}
