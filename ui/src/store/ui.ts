/** State that is about the interface, not about the run.
 *
 * Which screen is showing, which modal is open, what the console is filtering.
 * Deliberately separate from server state: the old UI kept both on one object,
 * so opening a modal and receiving a tool call were the same kind of change and
 * both repainted everything.
 */

import { create } from "zustand";

export type Screen = "home" | "plan" | "run" | "files" | "reviews";
export type Modal =
  | "settings" | "agents" | "models" | "loops" | "commands" | "memory" | null;

interface UiStore {
  sessionId: string | null;
  screen: Screen;
  modal: Modal;
  /** The step whose detail panel is open, if any. */
  openStep: string | null;
  /** Console filters. Reads are the bulk of the traffic and the least of the
   *  interest, so they are hidden by default. */
  showReads: boolean;
  hideFinished: boolean;
  /** Scope the console to one step, or null for the whole run. */
  consoleStep: string | null;
  /** The file open in the Files screen. */
  filePath: string | null;

  selectSession: (id: string | null) => void;
  go: (screen: Screen) => void;
  openModal: (modal: Modal) => void;
  setOpenStep: (stepId: string | null) => void;
  setConsoleStep: (stepId: string | null) => void;
  toggleReads: () => void;
  toggleHideFinished: () => void;
  openFile: (path: string | null) => void;
}

export const useUi = create<UiStore>((set) => ({
  sessionId: null,
  screen: "home",
  modal: null,
  openStep: null,
  showReads: false,
  hideFinished: false,
  consoleStep: null,
  filePath: null,

  // Switching session must drop everything scoped to the old one. Leaving the
  // open file and console scope behind is how the Files pane used to show the
  // previous project's code under the new project's name.
  selectSession: (id) => set({
    sessionId: id, openStep: null, consoleStep: null, filePath: null,
  }),
  go: (screen) => set({ screen }),
  openModal: (modal) => set({ modal }),
  setOpenStep: (openStep) => set({ openStep }),
  setConsoleStep: (consoleStep) => set({ consoleStep }),
  toggleReads: () => set((s) => ({ showReads: !s.showReads })),
  toggleHideFinished: () => set((s) => ({ hideFinished: !s.hideFinished })),
  openFile: (filePath) => set({ filePath }),
}));
