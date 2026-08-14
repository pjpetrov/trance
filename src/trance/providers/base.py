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

import json
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

ProviderKind = Literal["anthropic", "openai", "ollama", "llamacpp", "claudecode"]

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
    "claudecode": {
        "label": "Claude Code (subscription, local CLI)",
        # No endpoint and no key: it runs the `claude` binary on this machine
        # and bills against whatever that CLI is logged in to. Named rather than
        # left empty: empty takes the CLI's default, which is opus, on every
        # step including the small ones.
        "base_url": "",
        "model": "sonnet",
        "context_window": 200_000,
        "needs_key": False,
        "models": ["", "opus", "sonnet", "haiku"],
    },
}


class Cancelled(Exception):
    """A model call was deliberately broken off. Not a failure of the model."""


class BackendError(RuntimeError):
    """The model endpoint was unreachable or returned an error."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""
    #: True when `arguments` could not be parsed. Reporting an unparsable call
    #: as `{}` makes it indistinguishable from a call with no arguments, and
    #: the agent then gets a confusing "missing required argument" error
    #: instead of the real problem (usually output truncated at max_tokens).
    malformed: bool = False


#: Fields a provider attaches to its assistant message that must be sent back
#: with it. They are not ours to summarise or drop: the endpoint validates that
#: they came home.
ROUND_TRIP = ("reasoning_content", "reasoning_details", "thinking", "thought_signature")


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

    def replay(self, *, text: str | None = None, calls=None) -> dict:
        """This turn as a message to send back on the next request.

        The provider's own message is returned untouched whenever there is one,
        because parts of it have to come back exactly as they left. DeepSeek's
        thinking mode rejects a conversation whose earlier assistant turns
        arrive without `reasoning_content` — `null` is accepted, *absent* is a
        400 — so a rebuilt message has to carry those fields even when they are
        empty. Rebuilding is only for the paths where there is no usable message
        to return: a salvaged tool call, or a provider that sent none.
        """
        if self.raw_message and text is None:
            return self.raw_message

        rebuilt: dict = {"role": "assistant",
                         "content": text if text is not None else (self.text or "")}
        if calls is not None:
            rebuilt["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                for c in calls]
        elif self.raw_message.get("tool_calls"):
            rebuilt["tool_calls"] = self.raw_message["tool_calls"]
        for key in ROUND_TRIP:
            if key in self.raw_message:
                rebuilt[key] = self.raw_message[key]
        return rebuilt
    #: Set when the *endpoint* failed in a way the agent can recover from,
    #: rather than a transport error worth aborting the step for.
    provider_error: str = ""


@dataclass
class ModelPreset:
    """A model, with everything needed to call it. The one thing an agent picks.

    This used to be half a definition — a name pointing at a separately defined
    provider — so adding a model meant editing two places and remembering which
    endpoint it belonged to. A model now carries its own connection: the kind of
    API, the URL, the key. `provider` remains for configs written before that,
    and is what a model falls back to when it defines no endpoint of its own.
    """

    name: str
    provider: str = ""
    model: str = ""
    #: Which API this speaks. Empty = follow `provider`.
    kind: str = ""
    #: Endpoint. Empty with a kind set = that kind's default URL.
    base_url: str = ""
    api_key: str | None = None
    #: 0 means "inherit the provider's window".
    context_window: int = 0
    #: 0 means "inherit the agent defaults".
    max_tokens: int = 0
    #: What cuts a reply that goes long: "time" — the wall clock (right for a
    #: slow local model, where a 30k-token generation is half an hour) — or
    #: "size" — max output tokens, the classic cap (right for a fast endpoint,
    #: where seconds of wall clock say nothing). Empty means "time".
    cap: str = ""
    #: The wall-clock budget for one generation when `cap` is "time".
    #: 0 means "inherit the agent defaults" (600s).
    timeout_s: float = 0
    description: str = ""

    def __post_init__(self) -> None:
        if self.kind:
            defaults = KIND_DEFAULTS.get(self.kind, KIND_DEFAULTS["llamacpp"])
            self.base_url = self.base_url or defaults["base_url"]
            self.model = self.model or defaults["model"]
            self.context_window = self.context_window or defaults["context_window"]

    #: Kinds that reach their model without a URL — a local binary rather than
    #: an endpoint. They are as self-contained as it gets: there is nothing to
    #: borrow from a provider, and borrowing one sends them somewhere else
    #: entirely.
    LOCAL_KINDS = ("claudecode",)

    @property
    def self_contained(self) -> bool:
        """Whether this model defines its own endpoint rather than borrowing one."""
        return bool(self.kind) and (bool(self.base_url) or self.kind in self.LOCAL_KINDS)

    def as_provider(self) -> "ProviderConfig":
        """The connection this model implies, for the client factory."""
        return ProviderConfig(name=f"model:{self.name}", kind=self.kind,
                              base_url=self.base_url, model=self.model,
                              api_key=self.api_key, context_window=self.context_window)

    def to_dict(self, redact: bool = True) -> dict:
        data = asdict(self)
        if redact and data.get("api_key"):
            data["api_key"] = "***"
        data["has_key"] = bool(self.api_key)
        data["self_contained"] = self.self_contained
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ModelPreset":
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in known}
        if clean.get("api_key") == "***":      # the redacted placeholder echoed back
            clean.pop("api_key")
        return cls(**clean)


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


# --------------------------------------------------------------- cancelling

#: In-flight model calls, keyed by session. A local model can spend minutes on
#: one generation, and until this existed "stop" only took effect when that
#: generation finished — the button did nothing you could see.
_INFLIGHT: dict[str, list] = {}
_INFLIGHT_LOCK = threading.Lock()


def register_inflight(token: str, handle) -> None:
    """Record something abortable for `token`. Handles are per session."""
    if not token:
        return
    with _INFLIGHT_LOCK:
        _INFLIGHT.setdefault(token, []).append(handle)


def clear_inflight(token: str, handle) -> None:
    if not token:
        return
    with _INFLIGHT_LOCK:
        handles = _INFLIGHT.get(token) or []
        if handle in handles:
            handles.remove(handle)
        if not handles:
            _INFLIGHT.pop(token, None)


def abort_inflight(token: str) -> int:
    """Break off every model call this session has open. Returns how many.

    Shuts the socket down rather than closing it: shutdown unblocks the thread
    sitting in read() without freeing the descriptor, so there is no window in
    which the number could be reused by another connection.
    """
    with _INFLIGHT_LOCK:
        handles = list(_INFLIGHT.get(token) or [])
    aborted = 0
    for handle in handles:
        try:
            handle.abort()
            aborted += 1
        except Exception:                       # a race with a finished call
            continue
    return aborted


# ------------------------------------------------------------- discovery

#: Response shapes seen in the wild, in the order they are tried:
#:   {"data": [{"id": ...}]}     OpenAI, Ollama's /v1, OpenCode Zen
#:   {"models": [{"name": ...}]} llama-server's native listing
#:   {"data": [{"id": ...}]}     Anthropic, behind its own headers
def list_models(kind: str, base_url: str, api_key: str | None = None,
                timeout_s: float = 8.0) -> tuple[list[str], str]:
    """Ask an endpoint what it can run. Returns (models, note).

    An empty list with a note is the normal answer for a server that does not
    offer a listing, and is not an error: the model id stays free text, which
    is the only thing that works for an endpoint that will not say.
    """
    import json as _json
    import urllib.error
    import urllib.request

    base = (base_url or "").rstrip("/")
    if not base:
        return [], "no endpoint to ask"

    # Same reason as the chat client: a default urllib agent is refused
    # outright by some gateways before the request is even looked at.
    from ..worker.client import USER_AGENT

    if kind == "anthropic":
        url = f"{base}/v1/models" if not base.endswith("/v1") else f"{base}/models"
        headers = {"anthropic-version": "2023-06-01", "User-Agent": USER_AGENT}
        if api_key:
            headers["x-api-key"] = api_key
    else:
        url = f"{base}/models"
        headers = {"User-Agent": USER_AGENT}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

    try:
        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = _json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return [], f"{url} returned {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        return [], f"cannot reach {url}: {getattr(exc, 'reason', exc)}"
    except (ValueError, _json.JSONDecodeError):
        return [], f"{url} did not answer with JSON"

    entries = body.get("data") if isinstance(body, dict) else None
    if entries is None and isinstance(body, dict):
        entries = body.get("models")
    if not isinstance(entries, list):
        return [], f"{url} answered in a shape I do not recognise"

    names: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict):
            name = entry.get("id") or entry.get("name") or entry.get("model") or ""
        else:
            continue
        if name and name not in names:
            names.append(str(name))
    return sorted(names), (f"{len(names)} from {url}" if names else f"{url} listed none")
