"""Minimal OpenAI-compatible chat client.

One HTTP call, no SDK. Every backend we care about (llama-server, Ollama, vLLM,
OpenAI) speaks this shape, so the provider abstraction is just a base_url.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ..config import ModelConfig
from ..providers.base import BackendError, ChatResponse, ToolCall


class ChatClient:
    def __init__(self, config: ModelConfig):
        self.config = config
        self.endpoint = config.base_url.rstrip("/") + "/chat/completions"

    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf8"),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_s) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise BackendError(f"{self.endpoint} returned {exc.code}: {exc.read()[:400]!r}") from exc
        except urllib.error.URLError as exc:
            raise BackendError(
                f"cannot reach {self.endpoint} ({exc.reason}). Is the model server running? "
                f"Override with --base-url or TRANCE_BASE_URL."
            ) from exc

        return _parse(body)


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
    return ChatResponse(
        text=visible,
        tool_calls=tool_calls,
        finish_reason=choice.get("finish_reason", "stop"),
        usage=body.get("usage", {}) or {},
        reasoning=reasoning,
        raw_message=message,
    )
