# trance

A coding agent system built around one constraint: **minimize the context handed
to the model**, so smaller/cheaper models can work effectively on large
codebases.

The bet is that most of a coding agent's context window is waste. If you can
name the entry point of a task, a call graph tells you which ~15 functions
actually matter — and you can ship those instead of the twelve files they live
in.

## Status

| Component | Phase | State |
|---|---|---|
| Indexer (tree-sitter → SQLite symbol + call graph, incremental) | 1 | ✅ working |
| Context curator (N-hop walk → minimal bundle, token budget) | 1 | ✅ working |
| Trace layer (JSON Lines, schema'd, per-run directories) | 1 | ✅ working |
| Worker agent + lazy context tools | 2 | ✅ working |
| Orchestrator (curate → work → re-curate loop) | 2 | ✅ working |
| Multi-agent roles, remits, flow engine | 3 | ✅ working |
| Web UI: orchestrator chat, flow editor, live context inspector, steering | 3 | ✅ working |
| Per-agent providers/models | 3 | ✅ working |
| LSP-backed resolution (pyright / typescript-language-server) | 2 | 📋 designed, `src/trance/indexer/lsp.py` |
| Frontend ↔ backend linker (fetch/axios ↔ route handlers) | 2 | 📋 designed, `src/trance/linker/` |
| Call-graph visualization in the UI | 4 | 📋 designed, `ui/README.md` |

Remaining stubs are import-able, carry their design in their docstrings, and
raise `NotImplementedError` if called. Nothing is silently fake.

## The UI

```bash
trance serve                 # http://localhost:8080
trance serve --host 0.0.0.0  # network-visible; read the warning it prints
```

Three screens, matching how the work actually goes:

1. **Home** — name a project and its directory. Agents may only write inside it.
2. **Plan** — chat with the orchestrator. It asks a couple of questions, then
   proposes a team and an ordered flow via a tool call. The proposal lands in a
   drag-reorderable editor: change roles, rewrite tasks, set who verifies what,
   add or delete steps. Nothing runs until you press Run.
3. **Run** — the pipeline as a vertical flow with live status per step, and an
   activity log of every model call and tool call.

**Inspecting context.** Every `model_call` event carries the complete message
list that went to the model and the complete response that came back. The log
shows a one-line summary (model, round, token count); expanding gives the
verbatim prompt, message by message, plus reasoning and the response. That is
the answer to "what context did this agent actually get".

**Steering.** Queue a note onto the next prompt of one step or all pending
steps. Pause, resume, stop, rerun a step, skip a step — all mid-run. Edits to
pending steps are picked up by the engine between steps; steps that already ran
are immutable, because rewriting history would make the trace a lie.

**Remits.** Each role owns path globs. A backend agent writing to `frontend/`
is *refused at the tool boundary* and the refusal is reported to the model with
the name of the role that does own the path. That makes "an agent overstepping
another's duties" mechanical rather than a judgement call, and it raises a
`supervision` event in the UI.

**Per-agent providers.** Every agent picks a provider and model independently —
a big local model for the coder, a small fast one for the tester, a hosted one
elsewhere. The orchestrator is configured centrally in main settings. Each
provider carries its own `context_window`, and the runner budgets against it
per agent.

## Quickstart

```bash
uv sync --all-extras
uv pip install -e .

trance config --check                               # verify the model backend
trance index samples/sample-app                     # build the graph
trance run samples/sample-app get_user_orders \
    --task "add cursor-based pagination"            # the full agent
```

Inspecting the pieces individually:

```bash
trance symbols   samples/sample-app order           # find entry points
trance callgraph samples/sample-app get_user_orders --hops 4
trance bundle    samples/sample-app get_user_orders --task "..." --show
pytest
```

`trance index` writes `<repo>/.trance/graph.db`; `trance run` and `trance bundle`
write `runs/<run_id>/{run.json,trace.jsonl}`.

## Providers

A **provider** is a named model endpoint. Its `name` is the *shortname* you
attach to an agent; its `kind` selects the client:

| kind | client | notes |
|---|---|---|
| `anthropic` | official Anthropic SDK (`POST /v1/messages`) | needs an API key |
| `openai` | OpenAI-compatible `/chat/completions` | needs an API key |
| `ollama` | OpenAI-compatible, local | — |
| `llamacpp` | OpenAI-compatible, local (llama-server) | — |

Manage them from the **⚙ settings** panel — add, edit, enable/disable, test,
delete. `trance.toml` seeds the registry on first run; after that
`runs/providers.json` is the source of truth so UI edits persist. API keys are
write-only: the UI only ever sees `***`, and a blank key on save means
"unchanged", never "clear it".

### Models: what an agent actually picks

A **model** is a named `(provider, model id)` pair. It is the single thing you
assign to an agent — you don't choose an endpoint and a model separately,
because that's two decisions for what is really one:

| model | provider | model id | context |
|---|---|---|---|
| `smart` | claude | `claude-opus-5` | inherit (1M) |
| `cheap` | claude | `claude-haiku-4-5` | 200000 |
| `local` | llama | `unsloth/Qwen3.6-27B…` | inherit (64k) |

Credentials and the endpoint stay defined once on the provider; each model you
actually use gets its own handle. `smart` and `cheap` above share one Anthropic
connection and one API key.

Set `context window` on a model when it differs from the provider's default —
Haiku is 200k where Opus is 1M, and an over-sized budget means a 400 mid-run.
Leave it blank to inherit. The runner trims against whatever each agent
resolves to, so a 1M-context Claude agent and a 32k Ollama agent budget
independently in the same run.

Deleting is guarded in both directions: a provider that backs models won't
delete until they're repointed, and a model assigned to an agent won't delete
until it's freed.

**Anthropic is a real adapter, not a base-URL swap.** Its Messages API differs
in four ways that all have to be translated: `system` is a top-level parameter,
tools are `{name, description, input_schema}` rather than nested under
`function`, tool results come back as *user* messages with `tool_result` blocks
keyed by `tool_use_id`, and `temperature`/`top_p`/`top_k` are **rejected with a
400** on current models — so trance omits sampling parameters entirely for that
kind. `stop_reason: "refusal"` arrives as a successful 200 with empty content
and is surfaced as text rather than looking like an empty answer.

Tool calling is required for the lazy-context loop. Anthropic, llama.cpp's
`llama-server`, and Ollama all support it; where a model emits a tool call as
prose instead, `salvage_tool_calls` recovers it.

## Layout

```
src/trance/
  model.py            Symbol / Edge / ContextBundle dataclasses
  db.py               SQLite schema + graph queries (files, symbols, edges)
  indexer/
    languages.py      per-language grammars + tree-sitter queries — add a language here
    parse.py          source bytes -> symbols + raw call sites (resolution-free)
    resolve.py        call name -> symbol, with a confidence tag per edge
    service.py        repo walk, content-hash incrementality, delete detection
    lsp.py            PHASE 2: real language-server resolution
  curator/walker.py   entry point + N hops -> minimal ContextBundle
  linker/             PHASE 2: frontend<->backend HTTP edges
  worker/             PHASE 2: the agent that does the task + lazy context tools
  orchestrator/       PHASE 2: task -> curate -> work -> re-curate
  trace/writer.py     structured run traces
  cli.py              typer CLI

schemas/trace_event.schema.json   the observability contract
examples/runs/                    a real trace, committed for UI development
samples/sample-app/               TS frontend + FastAPI backend fixture
ui/                               PHASE 3
```

## Design decisions worth knowing

**Python for everything except the UI.** The indexer, curator, and orchestrator
are one process with one SQLite file — splitting them across languages buys
nothing at this scale. The UI is the natural place for TypeScript, and it talks
to trace files, not to a service.

**tree-sitter for syntax, LSP for semantics.** tree-sitter is fast, incremental,
and error-tolerant — right for *finding* definitions and call sites. It cannot
tell you which `create_user` a call refers to across modules or class
hierarchies; that's a type/scope question pyright already answers. The current
name-based resolver (`indexer/resolve.py`) is a placeholder that tags every edge
with how it was resolved (`same_file`, `same_dir`, `unique_global`, `ambiguous`,
`unresolved`), so the LSP rollout can be incremental and its impact measurable.

**Parsing is incremental; resolution is not.** Re-indexing an unchanged repo
parses zero files; a one-file edit parses one file. Resolution then re-runs
globally, because it's an in-memory name join that costs milliseconds — and
because a cross-file edit can invalidate edges in files that didn't change.
When resolution grows expensive (i.e. once it's LSP-backed), it gets the same
dirty-set treatment.

**The curator needs no LLM.** It's a graph walk with a budget check. Bodies for
near hops, signatures beyond `body_hops`, nearest-first trimming when the budget
binds, and the entry point is never dropped. Keeping it deterministic makes it
cheap, fast, and debuggable — and means the only model call that has to be smart
is the worker's.

**Under-fetching degrades into a tool call, not a hallucination.** Unresolved
call names ship *in the bundle* as a labeled list, so the worker knows what it's
missing and can ask for it via `get_definition()`. Every such call is traced;
a high tool-call rate is the signal that `max_hops` or the budget is too tight
for that task shape.

## Measured on the sample fixture

`get_user_orders`, 2 hops: 12 symbols from 3 files, ~663 tokens vs ~786 for the
whole files (16% smaller). On trance's own source the same walk saves ~49%. The
sample files are small on purpose; savings scale with the size of the files the
relevant functions happen to live in, which is exactly the regime real
repositories are in.

Token counts are a `len/4` estimate (`model.estimate_tokens`) and are labeled
`estimated` in traces. Swap in a real tokenizer before quoting absolute numbers.

## Known limitation: diffs from small models

The worker is prompted for a unified diff, and a 27B model does not reliably
produce one — observed failures are wrong `@@` line counts and hallucinated
context lines (`/users/...` for `/api/users/...`). The *reasoning* is sound and
the code is right; the patch format is what breaks. `patch -p1` rejects it.

The fix suits this architecture unusually well: **ask for whole-symbol
replacements instead of diffs.** The graph already stores exact `start_byte` /
`end_byte` for every symbol, so the worker can emit "here is the complete new
body of `OrderService.list_for_user`" and trance splices it in by byte range.
No line numbers to miscount, no context lines to hallucinate. Diff generation
then becomes trance's job, not the model's — and it is trivially correct.

## Next steps

1. **Symbol-level edits** — see above. Biggest usability win, and cheap.
2. **LSP resolution** — biggest correctness win. 248/359 edges in trance's own
   source are unresolved today; most are stdlib/builtin noise, but the ambiguous
   ones are the ones that silently mislead a curator.
3. **Frontend↔backend linker** — `fetchUserOrders` → `get_user_orders` in the
   sample fixture is the acceptance test.
4. **Inspection UI** — the trace data already exists; see `ui/README.md`.
