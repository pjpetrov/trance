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


class ChatClient:
    def __init__(self, config: ModelConfig):
        self.config = config
        self.endpoint = config.base_url.rstrip("/") + "/chat/completions"

    def complete(self, messages: list[dict], tools: list[dict] | None = None,
                 cancel_token: str = "", extra_body: dict | None = None) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
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
            body = self._send(request, handle)
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

    def _send(self, request, handle):
        """One request, retried while the endpoint says "later"."""
        for attempt in range(1, RETRIES + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.config.timeout_s) as resp:
                    handle.response = resp
                    return json.loads(resp.read())
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
        part for part in (message.get("reasoning_content") or "", inline_reasoning) if part
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
        reasoning=reasoning,
        # Kept whole, deliberately: see ChatResponse.replay().
        raw_message=message,
    )
