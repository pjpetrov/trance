"""Claude Code as a model backend.

The Claude Code CLI runs on a subscription rather than metered API credits, so
`claude -p` is a way to reach Opus without an API key. This client makes trance
talk to it the way it talks to any other model: messages in, text and tool calls
out.

Two things make that possible, both measured rather than assumed:

* `--system-prompt` replaces Claude Code's own preamble and `--tools ""` drops
  its built-in tools. Without them every call carries ~13,100 tokens of
  instructions trance did not write and does not want; with them, zero.
* Claude Code has no wire protocol for returning tool calls — it *executes*
  them. So the tools are described in the prompt and their calls come back as
  text, which is parsed here. trance has done this for years for local models
  with weak tool support; the same machinery applies.

That second point is the honest cost of this backend, and it bites harder than
"the model might not follow instructions". Claude writes tool calls in Claude's
own XML syntax because that is what it always writes, and the CLI *watches* for
that syntax — a turn that begins one comes back empty with stop_reason
"stop_sequence" and is_error true. So both forms are accepted: the fenced JSON
that is asked for, and the native syntax for when the habit wins.

Everything trance enforces — remits, context budgets, the graph — still runs on
trance's side, which is the whole reason not to let Claude Code do the work
itself.

Deliberately not: an MCP server. MCP servers are things Claude Code *calls*, so
they cannot make it a backend for something else — that is the opposite plumbing.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from typing import Any

from .base import BackendError, ChatResponse, ToolCall

#: What a call is allowed to take. A subscription model still thinks for a while
#: on a hard step, and the CLI starts a process each time.
DEFAULT_TIMEOUT_S = 600.0

#: The CLI sometimes gives up before it has sent anything: is_error, zero
#: duration, zero cost. It is not the prompt — the same conversation fails in a
#: burst and succeeds on its own a minute later, which is what a rate limit
#: looks like from the outside. So: wait, and try again.
ABORT_RETRIES = 4
ABORT_BACKOFF_S = 4.0

#: How the model is told to call a tool. One JSON object per call, fenced, so
#: it can be found in prose without a parser for the prose.
TOOL_PROTOCOL = """
You have tools. To call one, emit a fenced block exactly like this and nothing
else in it:

```tool_call
{"name": "read_file", "arguments": {"path": "src/main.js"}}
```

You may emit several blocks to make several calls in one turn. Do not describe
the call in prose instead of making it, and do not invent tools. When you have
finished and need no tool, answer normally with no block.

Use exactly the fenced form above. Do not use XML tags such as <invoke_tool>,
<function> or <parameter> — that syntax is intercepted before it reaches the
program running you, and the turn is thrown away.

The tools available to you:
"""

_BLOCK = re.compile(r"```tool_call\s*(\{.*?\})\s*```", re.DOTALL)

#: What the model reaches for when it forgets the instructions above: Claude's
#: own tool syntax. It writes this because it is what it always writes — and the
#: CLI watches for it, which is why a turn that starts one comes back with
#: stop_reason "stop_sequence" and nothing else. Accepted rather than fought:
#: the instruction stays, and this is here for when the instruction loses.
_NATIVE = re.compile(
    r"<(?:invoke_tool|function|antml:invoke)[^>]*>(.*?)</(?:invoke_tool|function|antml:invoke)\s*>",
    re.DOTALL)
_NATIVE_NAME = re.compile(r"<tool_name>\s*([\w.-]+)\s*</tool_name>|name=\"([\w.-]+)\"")
_NATIVE_PARAM = re.compile(
    r"<parameter\s+name=\"([\w.-]+)\"\s*>(.*?)</parameter\s*>", re.DOTALL)


def available() -> str:
    """The Claude Code binary, or "" when it is not installed."""
    return shutil.which("claude") or ""


class ClaudeCodeClient:
    """Speaks trance's client protocol; runs `claude -p` underneath."""

    def __init__(self, config):
        self.config = config
        self.model = config.model
        self.max_tokens = config.max_tokens
        self.binary = available()
        if not self.binary:
            raise BackendError(
                "the `claude` CLI is not on trance's PATH. Install Claude Code, or "
                "pick a different model.")

    # ------------------------------------------------------------------ api

    def complete(self, messages: list[dict], tools: list[dict] | None = None,
                 cancel_token: str = "") -> ChatResponse:
        last = ""
        for attempt in range(ABORT_RETRIES):
            try:
                return self._once(messages, tools)
            except _Aborted as stopped:
                last = str(stopped)
                if attempt + 1 < ABORT_RETRIES:
                    # Backing off properly rather than hammering: whatever is
                    # refusing wants time, not another identical request.
                    time.sleep(ABORT_BACKOFF_S * (2 ** attempt))
        raise BackendError(
            f"claude -p refused to send, {ABORT_RETRIES} times over "
            f"{int(ABORT_BACKOFF_S * (2 ** ABORT_RETRIES - 1))}s — no tokens, no cost, "
            f"no request. The same conversation goes through when it is left alone for "
            f"a while, so this is Claude Code throttling programmatic use rather than "
            f"anything wrong with the prompt. An agent loop makes five or more calls a "
            f"step, which is more than it will allow: use this model for the odd step "
            f"or as a backup, and an API model for the loop. ({last})")

    def _once(self, messages: list[dict], tools: list[dict] | None) -> ChatResponse:
        system, prompt = self._render(messages, tools)
        command = [self.binary, "-p", prompt,
                   "--output-format", "json",
                   "--system-prompt", system,
                   # Its own tools would edit the project directly, behind every
                   # remit and every counter trance keeps. trance's tools only.
                   "--tools", ""]
        if self.model:
            command += ["--model", self.model]

        try:
            done = subprocess.run(
                command, capture_output=True, text=True,
                # Without this the CLI waits three seconds for input that is
                # never coming, on every single call.
                stdin=subprocess.DEVNULL,
                timeout=self.config.timeout_s or DEFAULT_TIMEOUT_S)
        except subprocess.TimeoutExpired as exc:
            raise BackendError(
                f"claude -p did not answer within "
                f"{self.config.timeout_s or DEFAULT_TIMEOUT_S:.0f}s") from exc
        except OSError as exc:
            raise BackendError(f"could not run the claude CLI: {exc}") from exc

        if done.returncode != 0:
            if _is_abort(done.stdout):
                raise _Aborted("exited without sending")
            raise BackendError(_why(done.stderr or done.stdout, done.returncode))

        try:
            body = json.loads(done.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise BackendError(
                f"claude -p returned something that is not JSON: "
                f"{(done.stdout or '')[:200]}") from exc

        if body.get("is_error"):
            if _is_abort(done.stdout):
                raise _Aborted("stopped before sending")
            raise BackendError(_why(body.get("result") or "", done.returncode))

        text = body.get("result") or ""
        calls = self._tool_calls(text)
        return ChatResponse(
            text=_without_blocks(text) if calls else text,
            tool_calls=calls,
            finish_reason="tool_calls" if calls else (body.get("stop_reason") or "stop"),
            usage=_usage(body.get("usage") or {}),
            raw_message={"role": "assistant", "content": text},
        )

    # -------------------------------------------------------------- helpers

    def _render(self, messages: list[dict], tools: list[dict] | None) -> tuple[str, str]:
        """Flatten a conversation into the one system prompt and one prompt the
        CLI takes.

        Everything except the system message becomes the prompt, labelled, in
        order — a tool result is as much part of the conversation as a question,
        and dropping the labels is how a model loses track of who said what.
        """
        system_parts = [m.get("content") or "" for m in messages
                        if m.get("role") == "system" and isinstance(m.get("content"), str)]
        if tools:
            system_parts.append(TOOL_PROTOCOL + _describe(tools))
        system = "\n\n".join(p for p in system_parts if p).strip() or "You are a helpful assistant."

        lines: list[str] = []
        for message in messages:
            role = message.get("role")
            if role == "system":
                continue
            content = message.get("content")
            if isinstance(content, list):          # never sent by this path
                content = json.dumps(content)
            content = (content or "").strip()
            if role == "assistant":
                calls = message.get("tool_calls") or []
                if calls:
                    content = (content + "\n" + "\n".join(
                        f'```tool_call\n{{"name": "{c["function"]["name"]}", '
                        f'"arguments": {c["function"].get("arguments") or "{}"}}}\n```'
                        for c in calls)).strip()
                lines.append(f"[you said]\n{content}")
            elif role == "tool":
                lines.append(f"[result of {message.get('name') or 'a tool'}]\n{content}")
            else:
                lines.append(content)
        return system, "\n\n".join(lines).strip()

    def _tool_calls(self, text: str) -> list[ToolCall]:
        calls = self._fenced(text)
        return calls or self._native(text)

    def _native(self, text: str) -> list[ToolCall]:
        """Claude's own tool syntax, when it used that instead of ours."""
        calls: list[ToolCall] = []
        for n, block in enumerate(_NATIVE.finditer(text or "")):
            inner = block.group(1)
            named = _NATIVE_NAME.search(block.group(0))
            name = (named.group(1) or named.group(2)) if named else ""
            arguments = {key: value.strip()
                         for key, value in _NATIVE_PARAM.findall(inner)}
            if not name:
                continue
            calls.append(ToolCall(id=f"cc_native_{n}", name=name, arguments=arguments,
                                  raw_arguments=json.dumps(arguments)))
        return calls

    def _fenced(self, text: str) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for n, found in enumerate(_BLOCK.finditer(text or "")):
            raw = found.group(1)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                # Reported rather than dropped: the runner tells the model what
                # it got wrong, which is more use than silence.
                calls.append(ToolCall(id=f"cc_{n}", name="", arguments={},
                                      raw_arguments=raw, malformed=True))
                continue
            arguments = data.get("arguments")
            calls.append(ToolCall(
                id=f"cc_{n}", name=data.get("name") or "",
                arguments=arguments if isinstance(arguments, dict) else {},
                raw_arguments=json.dumps(arguments) if arguments is not None else "{}",
                malformed=not isinstance(arguments, dict) or not data.get("name"),
            ))
        return calls


def _describe(tools: list[dict]) -> str:
    """The tool schemas, as text, since there is nowhere else to put them."""
    lines = []
    for tool in tools:
        fn = tool.get("function") or tool
        params = json.dumps(fn.get("parameters") or {}, separators=(",", ":"))
        lines.append(f"- {fn.get('name')}: {fn.get('description') or ''}\n  arguments: {params}")
    return "\n".join(lines)


def _without_blocks(text: str) -> str:
    """The prose, minus whichever call syntax was used to carry the calls."""
    return _NATIVE.sub("", _BLOCK.sub("", text or "")).strip()


def _usage(raw: dict) -> dict[str, Any]:
    """Claude Code's counts, in the shape the rest of trance reads.

    Cache reads and creations are real input tokens — leaving them out would
    make this backend look free next to every other one.
    """
    prompt = (int(raw.get("input_tokens") or 0)
              + int(raw.get("cache_creation_input_tokens") or 0)
              + int(raw.get("cache_read_input_tokens") or 0))
    return {"prompt_tokens": prompt,
            "completion_tokens": int(raw.get("output_tokens") or 0),
            # Kept apart because they are not the same money: a cache read is
            # about a tenth of a fresh token, and for a delegated step it is
            # most of the number — the same conversation read back on every
            # internal turn.
            "cache_read_tokens": int(raw.get("cache_read_input_tokens") or 0)}


def _why(output: str, code: int) -> str:
    """The CLI's own complaint, which says what to do about it."""
    text = (output or "").strip()
    if "not logged in" in text.lower() or "authenticate" in text.lower():
        return "the claude CLI is not logged in — run `claude` once and sign in"
    if "usage limit" in text.lower() or "rate limit" in text.lower():
        return f"Claude Code says you are over its limit: {text[:200]}"
    return text[:300] or f"claude -p exited with status {code}"


class _Aborted(RuntimeError):
    """The CLI stopped before making a request. Retryable, and not the prompt."""


def _is_abort(stdout: str) -> bool:
    """A turn that never reached the model: no time on the wire, nothing spent.

    Those two are the signal, not the tokens — a refused request can still
    report cache reads. An answer that was cut off has duration and cost; this
    has neither, and the same conversation goes through a minute later.
    """
    try:
        body = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return False
    if not body.get("is_error"):
        return False
    return (not float(body.get("duration_api_ms") or 0)
            and not float(body.get("total_cost_usd") or 0))
