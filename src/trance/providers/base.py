"""Provider-neutral chat types and the provider registry.

A *provider* is a named model endpoint. Its `name` is the shortname you attach
to an agent; its `kind` decides which client implementation is used:

    anthropic   -> the official Anthropic SDK (POST /v1/messages)
    openai      -> OpenAI-compatible /chat/completions
    ollama      -> OpenAI-compatible, local
    llamacpp    -> OpenAI-compatible, local (llama.cpp llama-server)

Anthropic is genuinely a different wire protocol — different endpoint, auth
header, tool schema, and tool-result shape — so it gets its own client rather
than a base_url swap.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

ProviderKind = Literal["anthropic", "openai", "ollama", "llamacpp"]

#: Sensible starting points per kind, used when the UI creates a provider.
KIND_DEFAULTS: dict[str, dict[str, Any]] = {
    "anthropic": {
        "label": "Anthropic",
        "base_url": "https://api.anthropic.com",
        "model": "claude-opus-5",
        "context_window": 1_000_000,
        "needs_key": True,
        "models": ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5", "claude-opus-4-8"],
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "context_window": 128_000,
        "needs_key": True,
        "models": [],
    },
    "ollama": {
        "label": "Ollama (local)",
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5-coder:32b",
        "context_window": 32_768,
        "needs_key": False,
        "models": [],
    },
    "llamacpp": {
        "label": "llama.cpp",
        "base_url": "http://localhost:12345/v1",
        "model": "",
        "context_window": 64_000,
        "needs_key": False,
        "models": [],
    },
}


class BackendError(RuntimeError):
    """The model endpoint was unreachable or returned an error."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""


@dataclass
class ChatResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    #: Reasoning returned out-of-band (llama.cpp `reasoning_content`, Anthropic
    #: summarized thinking blocks). Traced, never fed back as context.
    reasoning: str = ""
    #: The assistant message to append to the conversation, in whatever shape
    #: the provider expects to receive back.
    raw_message: dict = field(default_factory=dict)


@dataclass
class ModelPreset:
    """A named (provider, model) pair — the single thing an agent picks.

    Selecting a provider *and* a model for every agent is two decisions where
    the user has one in mind ("give the tester the cheap local model"). A preset
    composes them under one shortname, so credentials and endpoint stay defined
    once on the provider while each model you actually use gets its own handle.
    """

    name: str
    provider: str
    model: str = ""
    #: 0 means "inherit the provider's window".
    context_window: int = 0
    #: 0 means "inherit the agent defaults".
    max_tokens: int = 0
    description: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ModelPreset":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class ProviderConfig:
    """A named model endpoint. `name` is the shortname agents refer to."""

    name: str = "default"
    kind: str = "llamacpp"
    label: str = ""
    base_url: str = ""
    model: str = ""
    api_key: str | None = None
    #: 0 is a sentinel meaning "use the kind's default". A real default here
    #: would silently win over the kind's, giving an Anthropic provider a 64k
    #: budget instead of 1M and mis-sizing every context trim.
    context_window: int = 0
    #: Disabled providers stay configured but don't appear in agent pickers.
    enabled: bool = True

    def __post_init__(self) -> None:
        defaults = KIND_DEFAULTS.get(self.kind, KIND_DEFAULTS["llamacpp"])
        self.base_url = self.base_url or defaults["base_url"]
        self.model = self.model or defaults["model"]
        self.label = self.label or f"{defaults['label']} ({self.name})"
        if not self.context_window:
            self.context_window = defaults["context_window"]

    def to_dict(self, redact: bool = True) -> dict:
        data = asdict(self)
        if redact and data.get("api_key"):
            data["api_key"] = "***"
        data["has_key"] = bool(self.api_key)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ProviderConfig":
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in known}
        # "***" is the redacted placeholder the UI echoes back; never store it.
        if clean.get("api_key") == "***":
            clean.pop("api_key")
        return cls(**clean)
