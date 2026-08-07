"""Anthropic provider, via the official SDK.

Anthropic's Messages API is not OpenAI-compatible, so this is a real adapter,
not a base_url swap. The four differences that matter:

1. `system` is a top-level parameter, not a message with role="system".
2. Tools are `{name, description, input_schema}` — not nested under `function`.
3. Tool results come back as a *user* message containing `tool_result` blocks,
   keyed by `tool_use_id`; OpenAI uses a dedicated `role: "tool"` message.
4. `temperature` / `top_p` / `top_k` are **rejected with a 400** on current
   models (Opus 5, Sonnet 5, Fable 5, Opus 4.8/4.7). We omit sampling
   parameters entirely rather than guessing which model accepts them.

`max_tokens` is required. `stop_reason: "refusal"` is a successful HTTP 200
with empty or partial content — it must be checked before reading content.
"""

from __future__ import annotations

import json
from typing import Any

from .base import BackendError, ChatResponse, ToolCall

#: Models whose safety classifiers can decline a request outright. On a refusal
#: the API returns 200 with stop_reason="refusal", not an error.
REFUSAL_STOP_REASON = "refusal"


#: Where the Messages API lives, before the SDK adds its own path.
ANTHROPIC_HOST = "https://api.anthropic.com"


def anthropic_base(base_url: str) -> str:
    """A base URL the Anthropic SDK will actually work with, or "" for default.

    The SDK appends `/v1/messages` itself, so a URL ending in `/v1` — which is
    what every other provider here wants, and therefore what gets typed — asks
    for `/v1/v1/messages` and comes back 404 "Not found". That is a confusing
    thing to debug from an error that mentions neither the URL nor the version,
    so the suffix is dropped rather than passed on.
    """
    trimmed = (base_url or "").strip().rstrip("/")
    if trimmed.endswith("/v1"):
        trimmed = trimmed[:-len("/v1")]
    return "" if not trimmed or trimmed == ANTHROPIC_HOST else trimmed


class AnthropicClient:
    def __init__(self, config):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise BackendError("the `anthropic` package is required for Anthropic providers") from exc

        self._sdk = anthropic
        kwargs: dict[str, Any] = {"timeout": config.timeout_s}
        if config.api_key:
            kwargs["api_key"] = config.api_key
        # A custom base_url supports gateways/proxies; the SDK default is fine
        # for api.anthropic.com, so only pass it when it's been changed.
        base = anthropic_base(config.base_url)
        if base:
            kwargs["base_url"] = base
        self.client = anthropic.Anthropic(**kwargs)
        self.config = config
        self.model = config.model
        self.max_tokens = config.max_tokens

    # ------------------------------------------------------------------ api

    def complete(self, messages: list[dict], tools: list[dict] | None = None,
                 cancel_token: str = "") -> ChatResponse:
        system, converted = split_system(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": converted,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [to_anthropic_tool(t) for t in tools]

        try:
            response = self.client.messages.create(**payload)
        except self._sdk.APIStatusError as exc:
            raise BackendError(f"Anthropic returned {exc.status_code}: {exc.message}") from exc
        except self._sdk.APIConnectionError as exc:
            raise BackendError(f"cannot reach the Anthropic API: {exc}") from exc

        return _parse(response)


def split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    """Pull system messages out and convert the rest to Anthropic blocks."""
    system_parts: list[str] = []
    converted: list[dict] = []

    for message in messages:
        role = message.get("role")
        if role == "system":
            system_parts.append(str(message.get("content") or ""))
        elif role == "tool":
            # OpenAI's dedicated tool message becomes a tool_result block on a
            # user turn. Consecutive results merge into one message, which is
            # what the API expects for parallel tool calls.
            block = {
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id", ""),
                "content": str(message.get("content") or ""),
            }
            if converted and converted[-1]["role"] == "user" and isinstance(converted[-1]["content"], list):
                converted[-1]["content"].append(block)
            else:
                converted.append({"role": "user", "content": [block]})
        elif role == "assistant":
            converted.append(_assistant_message(message))
        else:
            converted.append({"role": "user", "content": str(message.get("content") or "")})

    return "\n\n".join(p for p in system_parts if p), converted


def _assistant_message(message: dict) -> dict:
    """Rebuild an assistant turn, preserving tool_use blocks."""
    if message.get("_anthropic_content"):
        # Round-tripping our own parsed response: reuse the verbatim blocks.
        return {"role": "assistant", "content": message["_anthropic_content"]}

    blocks: list[dict] = []
    if message.get("content"):
        blocks.append({"type": "text", "text": str(message["content"])})
    for call in message.get("tool_calls") or []:
        fn = call.get("function", {})
        raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            args = {}
        blocks.append({
            "type": "tool_use", "id": call.get("id", ""), "name": fn.get("name", ""), "input": args,
        })
    return {"role": "assistant", "content": blocks or ""}


def to_anthropic_tool(spec: dict) -> dict:
    """OpenAI tool spec -> Anthropic tool spec."""
    fn = spec.get("function", spec)
    return {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        "input_schema": fn.get("parameters") or fn.get("input_schema")
        or {"type": "object", "properties": {}},
    }


def _parse(response) -> ChatResponse:
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    raw_blocks: list[dict] = []

    for block in response.content:
        kind = getattr(block, "type", None)
        if kind == "text":
            text_parts.append(block.text)
            raw_blocks.append({"type": "text", "text": block.text})
        elif kind == "thinking":
            # Empty unless display="summarized"; kept for the trace only.
            reasoning_parts.append(getattr(block, "thinking", "") or "")
            raw_blocks.append(block.model_dump() if hasattr(block, "model_dump") else {})
        elif kind == "tool_use":
            arguments = block.input if isinstance(block.input, dict) else {}
            tool_calls.append(ToolCall(
                id=block.id, name=block.name, arguments=arguments,
                raw_arguments=json.dumps(arguments),
            ))
            raw_blocks.append({
                "type": "tool_use", "id": block.id, "name": block.name, "input": arguments,
            })

    stop_reason = response.stop_reason or "stop"
    text = "".join(text_parts)
    if stop_reason == REFUSAL_STOP_REASON:
        detail = getattr(response, "stop_details", None)
        category = getattr(detail, "category", None) if detail else None
        text = text or (
            "The request was declined by Anthropic's safety classifiers"
            + (f" (category: {category})." if category else ".")
        )

    usage = getattr(response, "usage", None)
    return ChatResponse(
        text=text,
        tool_calls=tool_calls,
        finish_reason=stop_reason,
        usage={
            "prompt_tokens": getattr(usage, "input_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "output_tokens", 0) or 0,
        },
        reasoning="\n".join(p for p in reasoning_parts if p),
        # Echo blocks back verbatim on the next turn — the API validates that
        # every tool_result matches a tool_use from the same assistant message.
        raw_message={"role": "assistant", "content": "", "_anthropic_content": raw_blocks},
    )
