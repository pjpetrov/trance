/** The screens, driven the way a person drives them.
 *
 * These assert on what the *server was asked for*, not just that a render did
 * not throw. That is the whole gap the old harness left: it stubbed the DOM and
 * flattened text, so "the button is there" passed while "the button sends the
 * right thing to the right endpoint" was never checked once.
 */

import { describe, expect, it, beforeEach, afterEach, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RunScreen } from "@/screens/RunScreen";
import { PlanScreen } from "@/screens/PlanScreen";
import { AgentsEditor } from "@/modals/AgentsEditor";
import { ModelsEditor } from "@/modals/ModelsEditor";
import { SettingsPanel } from "@/modals/SettingsPanel";
import { useUi } from "@/store/ui";
import { fakeServer, renderWithQuery, stubWebSocket } from "./render";
import { config, event, eventsRoute, preset, role, session, step } from "./fixtures";

beforeEach(() => {
  stubWebSocket();
  useUi.setState({
    sessionId: "s1", screen: "run", modal: null, openStep: null,
    showReads: false, hideFinished: false, consoleStep: null, filePath: null,
  });
});

afterEach(() => vi.unstubAllGlobals());

describe("the run screen", () => {
  it("fetches a step's history only when that step is opened", async () => {
    const user = userEvent.setup();
    const server = fakeServer({
      "/api/sessions/s1": session({
        flow: { steps: [step({ id: "st1", task: "Build the maze renderer" })], cursor: 0 },
      }),
      "/api/sessions/s1/events": eventsRoute([]),
    });

    renderWithQuery(<RunScreen />);
    await screen.findByText(/Build the maze renderer/);

    // Nothing has been opened, so nothing step-scoped has been asked for. This
    // is the 13MB page load the old UI shipped by fetching every step up front.
    expect(server.calls.some((call) => call.url.includes("step=st1"))).toBe(false);

    await user.click(screen.getByText(/Build the maze renderer/));
    await waitFor(() =>
      expect(server.calls.some((call) => call.url.includes("step=st1"))).toBe(true));
  });

  it("says why a step with no trace is empty, rather than showing nothing", async () => {
    const user = userEvent.setup();
    fakeServer({
      "/api/sessions/s1": session(),
      "/api/sessions/s1/events": eventsRoute([]),
    });

    renderWithQuery(<RunScreen />);
    await user.click(await screen.findByText(/Build the maze renderer/));

    // "No calls recorded" reads as a broken panel; the truth is that the events
    // were never written, and only sessions predating the on-disk trace differ.
    expect(await screen.findByText(/never kept a trace|ran before this session kept a trace/i))
      .toBeInTheDocument();
  });

  it("renders live events through the same component as the step history", async () => {
    fakeServer({
      "/api/sessions/s1": session(),
      "/api/sessions/s1/events": eventsRoute([event({
        kind: "screenshot", shot: "st1/001.png", question: "does it look right?",
        checks: [], answer: "only one ghost is visible",
      })]),
    });

    renderWithQuery(<RunScreen />);
    expect(await screen.findByText(/only one ghost is visible/)).toBeInTheDocument();
  });
});

describe("the plan screen", () => {
  it("saves an edited step without a save button", async () => {
    const user = userEvent.setup();
    const server = fakeServer({
      "/api/sessions/s1": session(),
      "/api/agents": { agents: [role()], verifiers: [], toolsets: [] },
      "/api/loops": { loops: [] },
      "/api/sessions/s1/flow": { steps: [], team: [] },
    });

    renderWithQuery(<PlanScreen />);
    const field = await screen.findByDisplayValue("Build the maze renderer");

    await user.clear(field);
    await user.type(field, "Draw the ghosts");
    await user.tab();                       // blur commits the edit

    // A plan you edited and forgot to save is a plan that runs wrong.
    await waitFor(() => {
      const saved = server.to("/api/sessions/s1/flow").at(-1);
      expect(saved?.method).toBe("PUT");
      expect(JSON.stringify(saved?.body)).toContain("Draw the ghosts");
    });
  });
});

describe("the agents editor", () => {
  it("explains the browser capability where the switch is", async () => {
    const user = userEvent.setup();
    fakeServer({
      "/api/agents": { agents: [role({ name: "looker", toolsets: [], preset: "Qwen" })],
                       verifiers: [], toolsets: [] },
      "/api/presets": { presets: [] },
      "/api/config": config({ visual: { browser: true } }),
    });

    renderWithQuery(<AgentsEditor />);
    const browser = await screen.findByLabelText("browser");

    // Nothing about browsers until it is switched on.
    expect(screen.queryByText(/screenshots go to/)).not.toBeInTheDocument();

    await user.click(browser);
    // Ticking it answers immediately — finding out at run time costs a step.
    expect(await screen.findByText(/screenshots go to this agent's own model \(Qwen\)/))
      .toBeInTheDocument();
  });

  it("warns when the machine has no browser at all", async () => {
    const user = userEvent.setup();
    fakeServer({
      "/api/agents": { agents: [role({ name: "looker", toolsets: [] })],
                       verifiers: [], toolsets: [] },
      "/api/presets": { presets: [] },
      "/api/config": config({ visual: { browser: false } }),
    });

    renderWithQuery(<AgentsEditor />);
    await user.click(await screen.findByLabelText("browser"));
    expect(await screen.findByText(/No Chrome or Chromium/)).toBeInTheDocument();
  });

  it("says that an empty remit is read-only rather than unfinished", async () => {
    fakeServer({
      "/api/agents": { agents: [role({ name: "reviewer", paths: [] })],
                       verifiers: [], toolsets: [] },
      "/api/presets": { presets: [] },
      "/api/config": config(),
    });

    renderWithQuery(<AgentsEditor />);
    expect(await screen.findByText(/read-only, which is a choice/)).toBeInTheDocument();
  });

  it("stages edits and applies them all at once", async () => {
    const user = userEvent.setup();
    const server = fakeServer({
      "/api/agents": { agents: [role({ name: "frontend" })], verifiers: [], toolsets: [] },
      "/api/presets": { presets: [] },
      "/api/config": config(),
      "/api/agents/frontend": role(),
    });

    renderWithQuery(<AgentsEditor />);
    // Nothing pending, nothing to apply.
    const apply = await screen.findByRole("button", { name: /^Apply/ });
    expect(apply).toBeDisabled();
    expect(screen.getByText("No changes")).toBeInTheDocument();

    await user.type(await screen.findByDisplayValue("Frontend engineer"), "!");
    expect(await screen.findByText("1 unsaved change")).toBeInTheDocument();
    // Still nothing sent: an edit is a draft until it is applied.
    expect(server.to("/api/agents/frontend")).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: /^Apply/ }));
    await waitFor(() => {
      const saved = server.to("/api/agents/frontend").at(-1);
      expect(saved?.method).toBe("PUT");
      // A partial update would blank the prompt and the remit by omission.
      expect(saved?.body).toMatchObject({
        name: "frontend", system_prompt: expect.any(String),
        title: "Frontend engineer!",
      });
    });
  });

  it("discards every pending change, including an added agent", async () => {
    const user = userEvent.setup();
    const server = fakeServer({
      "/api/agents": { agents: [role({ name: "frontend" })], verifiers: [], toolsets: [] },
      "/api/presets": { presets: [] },
      "/api/config": config(),
    });

    renderWithQuery(<AgentsEditor />);
    await user.click(await screen.findByRole("button", { name: "New agent" }));
    expect(await screen.findByText("new-agent")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Discard" }));
    await waitFor(() => expect(screen.queryByText("new-agent")).not.toBeInTheDocument());
    // Backing out must not have touched the server on the way.
    expect(server.calls.filter((call) => call.method !== "GET")).toHaveLength(0);
  });

  it("adds a new agent with an editable name", async () => {
    const user = userEvent.setup();
    fakeServer({
      "/api/agents": { agents: [role({ name: "frontend" })], verifiers: [], toolsets: [] },
      "/api/presets": { presets: [] },
      "/api/config": config(),
    });

    renderWithQuery(<AgentsEditor />);
    // The old UI had no way to add one from here at all.
    await user.click(await screen.findByRole("button", { name: "New agent" }));

    const name = await screen.findByDisplayValue("new-agent");
    expect(name).toBeEnabled();                    // editable only while new
    await user.clear(name);
    await user.type(name, "dba");
    expect(await screen.findByText("1 unsaved change")).toBeInTheDocument();
  });
});

describe("the models editor", () => {
  it("can add a model, which it previously could not", async () => {
    const user = userEvent.setup();
    fakeServer({
      "/api/presets": { presets: [preset()] },
      "/api/config": config(),
    });

    renderWithQuery(<ModelsEditor />);
    await user.click(await screen.findByRole("button", { name: "New model" }));

    expect(await screen.findByDisplayValue("new-model")).toBeEnabled();
    expect(screen.getByText("1 unsaved change")).toBeInTheDocument();
  });

  it("will not test a model with unapplied edits", async () => {
    const user = userEvent.setup();
    fakeServer({ "/api/presets": { presets: [preset()] }, "/api/config": config() });

    renderWithQuery(<ModelsEditor />);
    expect(await screen.findByRole("button", { name: "Test" })).toBeEnabled();

    await user.type(await screen.findByDisplayValue("qwen3.6"), "x");
    // Testing sends a request to what is *saved*, so a pending edit would make
    // the result answer a different question than the one being asked.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Test" })).toBeDisabled());
  });
});

describe("settings", () => {
  it("no longer offers a second orchestrator model picker", async () => {
    fakeServer({ "/api/config": config() });
    renderWithQuery(<SettingsPanel />);
    // The agent card is the one place a model is chosen; this panel used to
    // offer a picker that disagreed with it.
    expect(await screen.findByText(/both on its agent card/)).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("reports what visual testing can do on this machine", async () => {
    fakeServer({ "/api/config": config({ visual: { browser: false } }) });
    renderWithQuery(<SettingsPanel />);
    expect(await screen.findByText(/No Chrome or Chromium on this machine/))
      .toBeInTheDocument();
    // And there is deliberately no vision-model picker: screenshots go to the
    // model the agent already has.
    expect(screen.queryByText(/vision model/i)).not.toBeInTheDocument();
  });

  it("saves a git toggle immediately", async () => {
    const user = userEvent.setup();
    const server = fakeServer({
      "/api/config": config(),
      "/api/config/planning": config().planning,
    });

    renderWithQuery(<SettingsPanel />);
    const box = await screen.findByLabelText(/Commit the project after every step/);
    await user.click(box);

    await waitFor(() => {
      const saved = server.to("/api/config/planning").at(-1);
      expect(saved?.body).toMatchObject({ git_commits: false });
    });
  });
});

describe("switching session", () => {
  it("drops everything scoped to the old one", () => {
    useUi.setState({ sessionId: "s1", openStep: "st9", consoleStep: "st9",
                     filePath: "src/game.js" });
    useUi.getState().selectSession("s2");

    // Leaving these behind is how the Files pane once showed the previous
    // project's code under the new project's name.
    const state = useUi.getState();
    expect(state).toMatchObject({
      sessionId: "s2", openStep: null, consoleStep: null, filePath: null,
    });
  });
});

describe("the console", () => {
  it("scopes to one step when asked", async () => {
    const user = userEvent.setup();
    fakeServer({
      "/api/sessions/s1": session(),
      "/api/sessions/s1/events": eventsRoute([
        event({ kind: "memory", note: "belongs to st1", stored: true }, { step_id: "st1" }),
        event({ kind: "memory", note: "belongs to st2", stored: true }, { step_id: "st2" }),
      ]),
    });

    renderWithQuery(<RunScreen />);
    expect(await screen.findByText(/belongs to st2/)).toBeInTheDocument();

    await user.click(screen.getByText(/Build the maze renderer/));
    await user.click(await screen.findByRole("button", { name: /focus console/ }));

    await waitFor(() =>
      expect(screen.queryByText(/belongs to st2/)).not.toBeInTheDocument());
    expect(screen.getByText(/belongs to st1/)).toBeInTheDocument();
  });
});

describe("empty states", () => {
  it("never shows a bare empty panel", async () => {
    fakeServer({
      "/api/sessions/s1": session({ flow: { steps: [], cursor: 0 } }),
      "/api/sessions/s1/events": eventsRoute([]),
    });

    const view = renderWithQuery(<RunScreen />);
    // An empty panel that does not explain itself reads as a bug.
    expect(await within(view.container).findByText(/Plan the work first/))
      .toBeInTheDocument();
    expect(screen.getByText(/shows what happens from now on/)).toBeInTheDocument();
  });
});
