# trance

A multi-agent coding harness built around one constraint: **keep the context
small**, so ordinary local models can work on real codebases.

The bet is that most of a coding agent's context window is waste. A 33KB file is
~8,400 tokens; the function you need is 150. If you can name what a task touches,
a call graph tells you which handful of symbols actually matter — and you can
ship those instead of the files they live in.

Everything else in trance follows from that: agents that fetch code by name,
edits that cost the size of the edit, a window gauge on screen while an agent
works, and a trace that shows you the exact prompt every agent received.

```
trance serve            # http://localhost:8080
```

---

## What it does

**Plans work.** You describe a project; an orchestrator asks a couple of
questions and proposes a team and an ordered flow. Edits to the plan save
themselves — there is no Save button to forget. Steps come with size
estimates, and anything too big to picture is broken up before you see it. The
plan lands in an editor — reorder, rewrite, add, delete — and nothing runs until
you press Run.

**Runs it, visibly.** A live console shows what the working agent is doing:
commands with their output, diffs as they are written, graph lookups, every
model call. The step that is running says how full its context window is.

**Keeps agents inside their lane.** Each agent owns path globs, a toolset, and a
command allowlist, enforced at the tool boundary rather than asked for in a
prompt. A backend agent writing to `frontend/` is refused, and told which agent
owns that path.

**Asks rather than refusing, when you are watching.** A refused write or command
pauses that agent and puts the exact action on screen: allow once, allow always
(which writes the decision into the policy), or refuse.

**Lets you steer mid-run.** Type a hint and it reaches the working agent on its
next round. Pause and click any console line to comment on that specific action.

**Commits as it goes.** Every step runs between two commits, so `git log` is the
record of what each agent did — and a step that goes wrong can be undone with
`git revert`, leaving both the work and the undo in history.

**Lets you review the result.** A Files screen with a real editor: read the code,
click a line number to comment on it, and send the review as a step the flow
runs. When it finishes, "what was fixed" answers from git. A page can be opened
in a browser from there — trance serves its folder as static files, on your
network as well as locally so you can look at it on a phone, and never builds
your project to do it. One button further, it can put that preview behind an
HTTPS tunnel so you can send the link to someone (see
[Sharing a preview](#sharing-a-preview-optional)).

---

## Screens

| | |
|---|---|
| **Home** | Name a project and its directory. Agents may only write inside it. |
| **Plan** | Orchestrator chat, and the flow editor underneath it. |
| **Run** | The pipeline, and a live console of the working agent. |
| **Files** | The project tree, a CodeMirror editor, and line-by-line review. |

Four modals: **⚙ Models**, **👥 Agents**, **↻ Loops**, **$_ Commands**.

---

## The pieces

### Models

A model is one definition: the API it speaks, where it lives, the key, and the
model id. Its name is the handle you attach to an agent — one thing to define,
one thing to pick.

| kind | client |
|---|---|
| `anthropic` | official Anthropic SDK (`POST /v1/messages`) |
| `openai` | OpenAI-compatible `/chat/completions` |
| `ollama` | OpenAI-compatible, local |
| `llamacpp` | OpenAI-compatible, local (llama-server) |

The model id offers what the endpoint says it has — trance asks `/models` and
handles the OpenAI, llama-server and Anthropic shapes — and stays free text when
an endpoint will not say, because a listing that misses a model should not lock
it out. **Test** sends a one-token probe and reports the URL it actually called.

### Agents

An agent is a name, a prompt, a **remit** (path globs it may write), a
**toolset**, a **command list**, a model, and optionally a **backup model**:

```
Model          Qwen3.6-llama.cpp — unsloth/Qwen3.6-27B-GGUF:IQ4_XS   tries [2]
Backup model   claude — claude-opus-5                                tries [2]
               4 tries in all — 2 on the model, then 2 on the backup.
```

The retry loop varies the prompt and the feedback; it never varies the model, so
an agent that fails the same way twice fails the same way a third time. The
backup is the switch for that, and an endpoint that returns 503 goes to it
immediately — a model that is down does not recover from being asked again.

Toolsets: `files` (read/write within the remit), `graph` (symbol lookups),
`commands` (allowlisted programs), `inspect` (file existence and size only — for
verifiers that must not be able to do the work themselves).

Ships with backend, frontend, tester, devops, reviewer, planner, factchecker and
the orchestrator. All editable; new ones can be added.

### Steps, loops and outcomes

A **step** is one agent attempting one task. It ends with an outcome the agent
states — `OUTCOME: SUCCESS` or `OUTCOME: FAILED — why` — and anything other than
success is retried, bounded by that agent's try count or the step's override.

A step may also carry a **check**: an independent agent confirming the report is
true. When a check contradicts a claimed success, the agent is told exactly what
the check found and tries again — a missing file is usually something forgotten,
and it can be finished. Only a check that keeps failing halts the run, because
later steps must not build on work that is not there. Every step that writes
files gets a factchecker by default.

A **loop** is a reusable block of agents wired by outcome — tester finds a bug,
developer fixes it, tester runs again, until it passes or a count runs out. Each
block's `SUCCESS`, `FAILED` and `CHECK FAILED` points at another block or leaves
the loop, and the number on an arrow bounds it. A step runs an agent *or* a loop.

An outcome can also take arrows in **tiers**, which is how a loop changes tactic
instead of repeating one:

```
on FAILED   1st–2nd  → developer                        [ ] backup model
            3rd–4th  → developer                        [x] backup model
            after 4, the loop halts
```

The first two failures go back to the developer as usual; the next two go back
to the same developer on its **backup model**, and the fifth stops. An ordinary
retry varies the prompt and never the model, so "try again" and "try again with
something stronger" are different arrows rather than the same one hoping for a
different result.

Deliberately not a general graph language: every arrow is labelled with an
outcome the engine already computes, so a loop cannot express a condition trance
has no way to evaluate.

### Keeping the context small

This is the point of the project, so it is worth being specific:

- **Symbols, not files.** A large indexed file answers `read_file` with its
  outline — the symbols it defines and their line ranges — and `get_definition`
  returns any of them in full. Five whole-file reads that cost 12,900 tokens
  cost 2,100 this way.
- **Edits, not rewrites.** `edit_file` replaces an exact snippet; `replace_symbol`
  swaps one function using the byte range the parser recorded. Changing a
  15-line function in a 1,187-line file: ~150 tokens instead of ~8,400.
- **Your own output stops costing.** A `write_file` call carries the whole file
  in its arguments and lives in the conversation forever; past writes keep their
  path and lose their content, since the bytes are on disk.
- **Repeats become pointers.** The same lookup twice answers with "you already
  have this above" — unless the earlier copy was trimmed away.
- **The budget is calibrated.** Trimming is enforced against the endpoint's own
  `prompt_tokens`, not a chars/4 guess that runs low exactly where code is dense.
- **Shared memory instead of re-derivation.** Agents write decisions others must
  match — a route shape, a port, the test command — to `.trance/memory.md`, which
  reaches every later agent and is compacted when it outgrows its budget.

### Git

Every block runs between two commits: a checkpoint before, and a commit of what
it did after. `revert on failure` is opt-in per step and per loop block, and
undoes with `git revert` rather than `git reset --hard` — so a reverted attempt
is still readable in history. Whatever was in the tree before the run is
committed first, so a revert can only ever take back the agent's own changes.

### The trace

Every model call, tool call, refusal, commit and outcome is an event. Events go
to the browser live and to `<session>/events.jsonl` on disk, so a finished step
is still explorable after a restart. The step detail groups them by the block
that produced them, folded, one section per attempt.

---

## Install

Requires Python 3.11+ and git. [uv](https://docs.astral.sh/uv/) recommended.

```bash
git clone https://github.com/pjpetrov/trance
cd trance
uv sync --all-extras
uv pip install -e .

trance serve
```

Point it at a model — locally, that is llama-server or Ollama already running;
otherwise add an API key in ⚙ Models.

```bash
trance serve -w ~/projects          # where new projects are created
trance serve --host 0.0.0.0         # network-visible; read the warning it prints
                                    # (file previews are network-visible either way)
trance serve --runs-dir ~/.trance   # where sessions and settings live
```

## Sharing a preview (optional)

The ▷ button in the Files tab serves a page's folder on your own network. To
send it to someone who is not on that network — "does this work on your phone?"
— trance can put it behind an HTTPS tunnel. This needs
[ngrok](https://ngrok.com/download), which is not a dependency: without it the
sharing controls simply do not appear.

```bash
# once, on the machine running trance
ngrok config add-authtoken <token from dashboard.ngrok.com>
```

Then in the UI: **▷** on a page to serve it, and **share…** next to *serving*.
trance starts ngrok on that preview's port, shows the public link, and gives you
**stop sharing** to close it again. Nothing is published until you press it, and
stopping the preview stops the tunnel with it.

Two things to know before you send the link on:

* **There is no password.** Anyone with the URL can read *every file in that
  folder*, not just the page. Keep secrets out of the folder you serve.
* **A browser sees ngrok's warning page first** ("You are about to visit…") and
  has to click through. That is ngrok's free plan; nothing here can turn it off.

From a terminal instead, with a password, which is the better default when the
folder is not just a game you want played:

```bash
tools/preview-tunnel.sh             # writes a policy with a generated password
                                    # on first run, and prints the credentials
tools/preview-tunnel.sh --open      # ...or no password, same as the UI button
tools/preview-tunnel.sh s_1a2b3c4d  # when several sessions are serving
```

A tunnel started this way shows up in the UI too — trance reads the ngrok
agent's local API, so the **share** link appears however the tunnel was started.
Set `TRANCE_NGROK_API` if your agent is not on the default port.

A Vite or webpack project will not work through any of this: the preview serves
files, and the page's bare imports (`import ... from "three"`) need a build step.
trance tells you which import stops it. Run your own dev server and point ngrok
at that port instead.

## The CLI

The UI is the main interface, but the pieces work on their own:

```bash
trance index      samples/sample-app                    # build the graph
trance symbols    samples/sample-app order              # find entry points
trance callgraph  samples/sample-app get_user_orders --hops 4
trance bundle     samples/sample-app get_user_orders --task "..." --show
trance run        samples/sample-app get_user_orders --task "add pagination"
trance config --check                                   # test the model backend
trance stats      samples/sample-app
```

`trance index` writes `<repo>/.trance/graph.db`. Runs write
`runs/<run_id>/{run.json,trace.jsonl}`, validated against
`schemas/trace_event.schema.json`.

## Tests

```bash
pytest                  # 470 tests
node tools/check_ui.js  # loads app.js in a DOM-less harness and drives it
```

The UI harness exists because parsing JavaScript proves nothing: it renders every
step status, every console entry kind, both file views and every modal, and
fails on a renderer that throws or an element that never appears. Several real
bugs in this repo were caught by making it see what a user sees rather than by
adding another `try`.

## Layout

```
src/trance/
  indexer/     tree-sitter parsing → SQLite symbol + call graph, incremental
  curator/     N-hop walk → minimal bundle under a token budget
  agents/      roles, tools, runner, memory, approvals, handoff
  providers/   model clients and the registry
  server/      FastAPI + websocket, and the whole UI in static/
  engine.py    the flow engine: steps, loops, checks, escalation
  loops.py     the loop state machine
  vcs.py       git checkpoints
tools/check_ui.js   the DOM-less UI harness
```

## Not done

Import-able, documented in their docstrings, and honest about it — they raise
`NotImplementedError` rather than pretending:

- **LSP-backed resolution** (`indexer/lsp.py`) — pyright / typescript-language-server
  instead of name matching, for cross-file resolution the parser cannot do.
- **Frontend ↔ backend linker** (`linker/`) — connecting `fetch` calls to the
  route handlers that serve them.
- **Call-graph visualization.** Every trace already carries what it needs:
  `graph_slice.nodes/edges` is the subset the curator walked, and each edge has
  a `resolution` (`same_file`, `ambiguous`, `unresolved`). Drawing the whole
  repo graph dimmed with that slice lit up — edges coloured by resolution, so
  the gaps in static analysis are visible — is the clearest statement of what
  this project is for. A curated slice is 10–50 nodes, so it needs no heavy
  graph library.

One known sharp edge: remit globs use `fnmatch`, whose `*` crosses `/`, so
`*.js` matches `server/app.js` as well as `app.js`. Narrow globs are unaffected.

## Licence

MIT — see [LICENSE](LICENSE).

Bundles [CodeMirror 5](https://codemirror.net/5/) (MIT) in
`src/trance/server/static/vendor/`, vendored rather than loaded from a CDN so
the UI works offline.
