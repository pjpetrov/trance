"""Minimal OpenAI-compatible chat client.

One HTTP call, no SDK. Every backend we care about (llama-server, Ollama, vLLM,
OpenAI) speaks this shape, so the provider abstraction is just a base_url.
"""

from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ..config import ModelConfig
from ..providers.base import (
    BackendError, Cancelled, ChatResponse, ToolCall, clear_inflight, register_inflight,
)


class _Abortable:
    """Holds an open response so another thread can break it off.

    urlopen blocks in read() until the model finishes generating, which for a
    local 27B is minutes. Closing the response from the stopping thread would
    free the file descriptor while this one may still touch it; shutting the
    socket down unblocks the read and leaves the descriptor ours to close.
    """

    def __init__(self):
        self.response = None
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True
        response = self.response
        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


#: Sent on every request. urllib's default is "Python-urllib/3.x", which
#: Cloudflare blocks outright — a working curl and a failing trance against the
#: same URL, answered with "error code: 1010". Identifying honestly as trance is
#: both the fix and the right thing to send.
USER_AGENT = "trance/0.1 (+https://github.com/trance)"

#: Statuses that mean "ask again later", not "you asked wrong". A 401 is a bad
#: key and retrying it is pointless; a 503 is a busy gateway and giving up on
#: the first one throws away a step for something that clears in a second.
TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 522, 524})
#: Three tries, ~1s then ~2s apart. Long enough for a gateway hiccup, short
#: enough that a genuinely down endpoint is reported rather than waited on.
RETRIES = 3
BACKOFF_S = 1.0

#: How long the stream may go completely silent before the call is failed.
#: With streaming, tokens arrive as they are generated, so silence no longer
#: means "a long generation" — it means the server stopped talking to us.
#: Prompt processing is the longest legitimate silence: a full 46k-token
#: prompt takes tens of seconds on one GPU before the first token appears.
IDLE_TIMEOUT_S = 180.0
#: At most one progress report per second — enough for a live console line,
#: nowhere near one event per token.
PROGRESS_EVERY_S = 1.0


class ChatClient:
    #: Tells the runner it may pass `on_progress` — fakes and non-streaming
    #: clients don't advertise it and are called exactly as before.
    supports_progress = True

    def __init__(self, config: ModelConfig):
        self.config = config
        self.endpoint = config.base_url.rstrip("/") + "/chat/completions"

    def complete(self, messages: list[dict], tools: list[dict] | None = None,
                 cancel_token: str = "", extra_body: dict | None = None,
                 on_progress=None) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            # Streamed so the generation is observable while it happens and so
            # a runaway one can be cut on time spent rather than size. The
            # final chunk still reports token usage.
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        # Endpoint-specific knobs the caller knows this backend accepts. Kept
        # opt-in and passed only where it was checked: a strict gateway answers
        # 400 to a body field it does not recognise, so "harmless if ignored" is
        # not a safe assumption to make on the caller's behalf.
        if extra_body:
            payload.update(extra_body)

        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                **({"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {}),
            },
            method="POST",
        )
        handle = _Abortable()
        register_inflight(cancel_token, handle)
        try:
            body = self._send(request, handle, on_progress)
        except urllib.error.HTTPError as exc:
            body = getattr(exc, "trance_body", None)
            if body is None:
                body = exc.read().decode("utf8", errors="replace")
            if _is_truncated_tool_call(body):
                # llama.cpp parses tool arguments itself and 500s on a call the
                # model did not finish writing. That is the same "ran out of
                # output tokens mid-argument" problem we already handle when the
                # partial JSON reaches us — recoverable, not fatal to the step.
                return ChatResponse(text="", finish_reason="length",
                                    provider_error="truncated_tool_call")
            if "image(s) may be provided" in (body or ""):
                # vLLM served without a vision encoder, or with its
                # per-prompt image limit below what we sent. Measured live:
                # "--language-model-only" makes the limit 0, so every
                # screenshot the user attached came back as a bare 400.
                raise BackendError(
                    f"{self.endpoint} refused the screenshots: {body[:160]}. "
                    f"On vLLM, drop --language-model-only (it loads the model "
                    f"without its vision encoder) and set "
                    f"--limit-mm-per-prompt '{{\"image\": 8}}' to allow "
                    f"several pictures in one prompt."
                ) from exc
            if "enable-auto-tool-choice" in (body or ""):
                # vLLM without tool calling switched on. Retrying cannot help
                # and the raw 400 sends people reading trance's request instead
                # of their server command line — say the actual fix.
                raise BackendError(
                    f"{self.endpoint} is a vLLM server started without tool "
                    f"calling. Restart it with --enable-auto-tool-choice and "
                    f"--tool-call-parser hermes (qwen3_coder for Qwen3-Coder "
                    f"models); add --reasoning-parser qwen3 for structured "
                    f"thinking. Agents cannot run tools against it until then."
                ) from exc
            raise BackendError(f"{self.endpoint} returned {exc.code}: {body[:400]}") from exc
        except urllib.error.URLError as exc:
            if handle.aborted:
                raise Cancelled("the model call was stopped") from exc
            raise BackendError(
                f"cannot reach {self.endpoint} ({exc.reason}). Is the model server running? "
                f"Override with --base-url or TRANCE_BASE_URL."
            ) from exc
        except (OSError, ValueError) as exc:
            # A shutdown socket surfaces as a read error rather than a URLError.
            if handle.aborted:
                raise Cancelled("the model call was stopped") from exc
            raise BackendError(f"{self.endpoint} failed: {exc}") from exc
        finally:
            clear_inflight(cancel_token, handle)

        return _parse(body)

    def _send(self, request, handle, on_progress=None):
        """One request, retried while the endpoint says "later".

        The socket timeout is per read, which on a streamed response makes it
        an *idle* timeout: a generation may run as long as it keeps producing
        tokens, and only a server that has gone silent trips it. The overall
        limit on a generation is `timeout_s` of wall clock, enforced in
        `_read_stream` — exceeding it cuts the reply and keeps the partial
        rather than erroring, because by then real tokens were paid for.
        """
        for attempt in range(1, RETRIES + 1):
            try:
                with urllib.request.urlopen(request, timeout=IDLE_TIMEOUT_S) as resp:
                    handle.response = resp
                    headers = getattr(resp, "headers", None)
                    kind = str(headers.get("Content-Type", "") if headers else "")
                    if "text/event-stream" not in kind:
                        # A gateway that ignored `stream: true` and answered
                        # whole. Same shape as before streaming existed.
                        return json.loads(resp.read())
                    return self._read_stream(resp, on_progress)
            except urllib.error.HTTPError as exc:
                # The body can only be read once, and both the truncation check
                # here and the error message later need it.
                exc.trance_body = exc.read().decode("utf8", errors="replace")
                last = exc
                if exc.code not in TRANSIENT_STATUS or attempt == RETRIES:
                    raise
                # A 500 carrying a truncated tool call is the model's doing, not
                # the server's — retrying sends the same oversized reply again.
                if exc.code == 500 and _is_truncated_tool_call(exc.trance_body):
                    raise
            except urllib.error.URLError as exc:
                last = exc
                if handle.aborted or attempt == RETRIES:
                    raise
            time.sleep(BACKOFF_S * (2 ** (attempt - 1)))
        raise last

    def _read_stream(self, resp, on_progress=None):
        """Assemble the streamed chunks back into one response body.

        Reassembled into the non-streaming shape and handed to the same
        `_parse` as ever, so streaming changes when we see the reply, not what
        anyone downstream receives.

        Two things only a stream makes possible happen here:
        - `on_progress` is called about once a second with what has arrived so
          far, which is how the console shows a think while it is thought;
        - a generation that outlives `timeout_s` is cut and returned as
          `finish_reason: "time"` with everything produced up to the cut —
          the time-based analogue of the server's own `"length"`. Closing the
          connection is the cut: llama.cpp stops generating on disconnect.
        """
        started = time.monotonic()
        # The preset chooses what cuts a long reply. Capped by size, the wall
        # clock never cuts: the server's max_tokens is the only limit, and the
        # idle timeout alone guards against a server gone silent.
        by_time = (getattr(self.config, "cap", "time") or "time") != "size"
        deadline = started + self.config.timeout_s if by_time else None
        reasoning: list[str] = []
        content: list[str] = []
        tool_calls: dict[int, dict] = {}
        finish, usage, chunks, cut = None, {}, 0, False
        last_report = 0.0

        for raw in resp:
            line = raw.decode("utf8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            error = chunk.get("error")
            if error:
                # Mid-stream, failures arrive as a frame, not a status code.
                # The one recoverable kind is the same one the non-streaming
                # path knows: a tool call the model never finished writing.
                text = error if isinstance(error, str) else json.dumps(error)
                if _is_truncated_tool_call(text):
                    return {"choices": [{"message": {}, "finish_reason": "length"}],
                            "usage": usage, "_provider_error": "truncated_tool_call"}
                raise BackendError(f"{self.endpoint} streamed an error: {text[:400]}")
            if chunk.get("usage"):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            if choices:
                chunks += 1
                choice = choices[0]
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
                delta = choice.get("delta") or {}
                # llama.cpp streams the think as `reasoning_content`; vLLM's
                # reasoning parser streams the same thing as `reasoning`.
                thought = delta.get("reasoning_content") or delta.get("reasoning")
                if thought:
                    reasoning.append(thought)
                if delta.get("content"):
                    content.append(delta["content"])
                for part in delta.get("tool_calls") or []:
                    slot = tool_calls.setdefault(
                        int(part.get("index") or 0), {"id": "", "name": "", "arguments": []})
                    if part.get("id"):
                        slot["id"] = part["id"]
                    fn = part.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"].append(fn["arguments"])

            now = time.monotonic()
            if on_progress and now - last_report >= PROGRESS_EVERY_S:
                last_report = now
                text = "".join(content)
                thought = "".join(reasoning)
                answering = bool(text or tool_calls)
                try:
                    on_progress({
                        "phase": "answering" if answering else "thinking",
                        "reasoning_chars": len(thought),
                        "text_chars": len(text),
                        # One SSE chunk per generated token on every backend
                        # we stream from, so this is the live token count.
                        "tokens": chunks,
                        "elapsed_s": round(now - started, 1),
                        "tail": (text if answering else thought)[-500:],
                    })
                except Exception:
                    pass  # a broken progress line must not kill the call
            if deadline is not None and now >= deadline:
                cut = True
                break

        # The role is not optional: this message is replayed into the next
        # request's conversation, and llama.cpp 500s the whole request over a
        # message without one ("Failed to parse messages: Missing 'role'").
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if tool_calls:
            message["tool_calls"] = [
                {"id": slot["id"] or f"call_{index}", "type": "function",
                 "function": {"name": slot["name"],
                              "arguments": "".join(slot["arguments"])}}
                for index, slot in sorted(tool_calls.items())]
        return {
            "choices": [{"message": message,
                         "finish_reason": "time" if cut else (finish or "stop")}],
            "usage": usage,
        }


#: A server-side complaint that the model's tool arguments were cut off.
_TRUNCATED_ARGS = re.compile(
    r"parse tool call arguments|missing closing quote|unexpected end of input"
    r"|invalid string.*last read", re.IGNORECASE | re.DOTALL)


def _is_truncated_tool_call(body: str) -> bool:
    return bool(_TRUNCATED_ARGS.search(body or ""))


#: Start of something that might be a tool invocation. The extent is found by
#: brace matching, not regex — a nested "arguments" object defeats any pattern.
_TOOL_START = re.compile(r"\{\s*\"(?:name|function)\"\s*:")


def _json_objects(text: str):
    """Yield candidate JSON objects, respecting nesting and quoted braces."""
    for match in _TOOL_START.finditer(text):
        depth, in_string, escaped = 0, False, False
        for index in range(match.start(), len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    yield text[match.start() : index + 1]
                    break


#: Qwen and other Hermes-template models emit calls as XML when the structured
#: channel is not used: <function=name><parameter=key>value</parameter></function>
_XML_FUNCTION = re.compile(r"<function=([\w.-]+)\s*>(.*?)</function\s*>", re.DOTALL)
_XML_PARAM = re.compile(r"<parameter=([\w.-]+)\s*>(.*?)</parameter\s*>", re.DOTALL)


def _xml_tool_calls(text: str, known_names: set[str]) -> list[ToolCall]:
    recovered: list[ToolCall] = []
    for match in _XML_FUNCTION.finditer(text):
        name = match.group(1)
        if name not in known_names:
            continue
        args = {key: value.strip() for key, value in _XML_PARAM.findall(match.group(2))}
        recovered.append(ToolCall(
            id=f"salvaged_xml_{len(recovered)}", name=name, arguments=args,
            raw_arguments=json.dumps(args)))
    return recovered


def salvage_tool_calls(text: str, known_names: set[str]) -> list[ToolCall]:
    """Recover tool calls a model emitted as prose instead of structured calls.

    Some chat templates (notably several Ollama-packaged models) will happily
    print `{"name": "write_file", "arguments": {...}}` into the content field
    while leaving `tool_calls` empty. Without this the call is silently lost:
    the agent believes it wrote a file and nothing happened.

    Only names the agent was actually offered are accepted, so this cannot
    invent a tool the role does not have.
    """
    if not text:
        return []
    xml = _xml_tool_calls(text, known_names)
    if xml:
        return xml
    recovered: list[ToolCall] = []
    for blob in _json_objects(text):
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(data.get("function"), dict):  # OpenAI-shaped, printed as text
            data = data["function"]
        name = data.get("name")
        if name not in known_names:
            continue
        args = data.get("arguments") or data.get("parameters") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                continue
        if not isinstance(args, dict):
            continue
        recovered.append(
            ToolCall(id=f"salvaged_{len(recovered)}", name=name, arguments=args,
                     raw_arguments=json.dumps(args))
        )
    return recovered


#: Tags models use to mark internal reasoning inside `content`. Some templates
#: route it to `reasoning_content`; others just write it inline, where it ends
#: up shown to the user as if it were the answer.
_THINK_TAGS = ("think", "thinking", "reasoning", "scratchpad")
_THINK_BLOCK = re.compile(
    r"<(" + "|".join(_THINK_TAGS) + r")\b[^>]*>(.*?)</\1\s*>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN = re.compile(
    r"<(" + "|".join(_THINK_TAGS) + r")\b[^>]*>(.*)$", re.DOTALL | re.IGNORECASE)


def split_reasoning(content: str) -> tuple[str, str]:
    """Separate inline reasoning from the answer.

    Returns (visible, reasoning). An *unclosed* tag means the response was cut
    off mid-thought — everything after it is reasoning, and the visible part is
    empty, which is the honest answer rather than showing half a thought.
    """
    if not content:
        return "", ""
    thoughts: list[str] = []

    def take(match):
        thoughts.append(match.group(2).strip())
        return ""

    visible = _THINK_BLOCK.sub(take, content)
    unclosed = _THINK_OPEN.search(visible)
    if unclosed:
        thoughts.append(unclosed.group(2).strip())
        visible = visible[: unclosed.start()]
    return visible.strip(), "\n\n".join(t for t in thoughts if t)


def _parse(body: dict) -> ChatResponse:
    try:
        choice = body["choices"][0]
    except (KeyError, IndexError) as exc:
        raise BackendError(f"malformed response: {json.dumps(body)[:400]}") from exc
    message = choice.get("message", {})

    tool_calls = []
    for call in message.get("tool_calls") or []:
        fn = call.get("function", {})
        raw_args = fn.get("arguments") or "{}"
        malformed = False
        try:
            args = json.loads(raw_args)
            if not isinstance(args, dict):
                args, malformed = {}, True
        except json.JSONDecodeError:
            args, malformed = {}, True
        tool_calls.append(ToolCall(
            id=call.get("id", ""), name=fn.get("name", ""), arguments=args,
            raw_arguments=raw_args, malformed=malformed,
        ))

    visible, inline_reasoning = split_reasoning(message.get("content") or "")
    reasoning = "\n\n".join(
        part for part in (message.get("reasoning_content") or message.get("reasoning") or "",
                          inline_reasoning) if part
    )

    # A turn the model spent no thought on comes back as `"reasoning_content":
    # null`, and DeepSeek then rejects that same null on the next request:
    # "the `reasoning_content` in the thinking mode must be passed back to the
    # API". Checked against the endpoint — absent is accepted, a string is
    # accepted, null is the one shape that 400s. So a null is filled in with
    # whatever thinking there was, and an empty string when there was none.
    if "reasoning_content" in message and message["reasoning_content"] is None:
        message = {**message, "reasoning_content": reasoning}
    return ChatResponse(
        text=visible,
        tool_calls=tool_calls,
        finish_reason=choice.get("finish_reason", "stop"),
        usage=body.get("usage", {}) or {},
        provider_error=body.get("_provider_error", ""),
        reasoning=reasoning,
        # Kept whole, deliberately: see ChatResponse.replay().
        raw_message=message,
    )
