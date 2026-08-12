/** State that is about the interface, not about the run.
 *
 * Which screen is showing, which modal is open, what the console is filtering.
 * Deliberately separate from server state: the old UI kept both on one object,
 * so opening a modal and receiving a tool call were the same kind of change and
 * both repainted everything.
 */

import { create } from "zustand";

export type Screen = "home" | "plan" | "run" | "files" | "reviews" | "commits" | "stats";
export type Modal =
  | "settings" | "agents" | "models" | "loops" | "commands" | "memory" | null;

interface UiStore {
  sessionId: string | null;
  screen: Screen;
  modal: Modal;
  /** The step whose detail panel is open, if any. */
  openStep: string | null;
  /** A step the plan page should scroll to and flash, set by another page. */
  planFocus: string | null;
  /** Set when another page chose the step deliberately — the run page's
   *  "open whatever is running" landing logic must stand down for it. */
  stepPinned: boolean;
  /** Whether the console follows the run: the newest lines stay in view, the
   *  open step moves to whatever is working, and the newest run is the one
   *  shown. It was implicit before, and something implicit that sometimes
   *  stops is indistinguishable from something broken. */
  follow: boolean;
  /** Console filters. Reads are the bulk of the traffic and the least of the
   *  interest: on one measured step, 386 of an agent's 407 tool calls were
   *  lookups and 15 were edits, so the work was buried in them. Hidden by
   *  default now — the button says how many it is holding back. */
  showReads: boolean;
  hideFinished: boolean;
  /** Scope the console to one step, or null for the whole run. */
  consoleStep: string | null;
  /** The file open in the Files screen. */
  filePath: string | null;
  /** The orchestrator reply whose commits the Commits screen is showing. A
   *  request leads to a plan, a run and then commits, and this is the thread
   *  back: it is set by pressing "what came of this" on the reply itself. */
  commitsFor: string | null;

  selectSession: (id: string | null) => void;
  go: (screen: Screen) => void;
  openModal: (modal: Modal) => void;
  setOpenStep: (stepId: string | null) => void;
  focusPlanStep: (stepId: string | null) => void;
  /** Open the run page on exactly this step. */
  showStep: (stepId: string) => void;
  unpinStep: () => void;
  setFollow: (follow: boolean) => void;
  setConsoleStep: (stepId: string | null) => void;
  toggleReads: () => void;
  toggleHideFinished: () => void;
  openFile: (path: string | null) => void;
  showCommitsFor: (messageId: string) => void;
}

export const useUi = create<UiStore>((set) => ({
  sessionId: null,
  screen: "home",
  modal: null,
  openStep: null,
  planFocus: null,
  stepPinned: false,
  follow: true,
  showReads: false,
  hideFinished: false,
  consoleStep: null,
  filePath: null,
  commitsFor: null,

  // Switching session must drop everything scoped to the old one. Leaving the
  // open file and console scope behind is how the Files pane used to show the
  // previous project's code under the new project's name.
  selectSession: (id) => set({
    sessionId: id, openStep: null, consoleStep: null, filePath: null,
    commitsFor: null,
  }),
  go: (screen) => set({ screen }),
  openModal: (modal) => set({ modal }),
  setOpenStep: (openStep) => set({ openStep }),
  // Clearing the focus must not yank the user back to the plan page.
  focusPlanStep: (planFocus) =>
    set(planFocus ? { planFocus, screen: "plan" } : { planFocus }),
  showStep: (openStep) => set({ openStep, stepPinned: true, screen: "run" }),
  unpinStep: () => set({ stepPinned: false }),
  setFollow: (follow) => set({ follow }),
  setConsoleStep: (consoleStep) => set({ consoleStep }),
  toggleReads: () => set((s) => ({ showReads: !s.showReads })),
  toggleHideFinished: () => set((s) => ({ hideFinished: !s.hideFinished })),
  openFile: (filePath) => set({ filePath }),
  showCommitsFor: (commitsFor) => set({ commitsFor, screen: "commits" }),
}));
