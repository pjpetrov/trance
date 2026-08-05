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

    return ChatResponse(
        text=message.get("content") or "",
        tool_calls=tool_calls,
        finish_reason=choice.get("finish_reason", "stop"),
        usage=body.get("usage", {}) or {},
        reasoning=message.get("reasoning_content") or "",
        raw_message=message,
    )
