"""Configuration: defaults <- trance.toml <- environment <- CLI flags.

Providers are named OpenAI-compatible endpoints. A role picks a provider by
name and may override the model, so one run can mix a big local model for the
coder, a small fast one for the tester, and a hosted one for the orchestrator.

`context_window` is per provider and is not optional in practice: it is the
budget the agent runner trims tool results against. Getting it wrong means a
500 from the server mid-run.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field, fields

from .providers.base import ModelPreset, ProviderConfig
from pathlib import Path

CONFIG_FILENAME = "trance.toml"


@dataclass
class ModelConfig:
    """Fully resolved settings for one agent's model calls."""

    base_url: str = "http://localhost:12345/v1"
    model: str = "unsloth/Qwen3.6-27B-GGUF:IQ4_XS"
    api_key: str | None = None
    temperature: float = 0.0
    max_tokens: int = 4096
    timeout_s: float = 600.0
    max_tool_rounds: int = 8
    context_window: int = 64000
    provider: str = "default"
    #: Which client implementation to use (see providers/).
    kind: str = "llamacpp"

    @property
    def input_budget(self) -> int:
        """Tokens available for the prompt, leaving room to generate."""
        return max(2000, self.context_window - self.max_tokens - 1000)


@dataclass
class CuratorSettings:
    max_hops: int = 2
    body_hops: int = 1
    token_budget: int = 8000
    include_callers: bool = False
    skip_ambiguous: bool = False
    include_module_constants: bool = True


@dataclass
class AgentDefaults:
    """Defaults applied to any role that doesn't override them."""

    #: Preferred: names a preset (provider + model together).
    preset: str | None = None
    provider: str = "default"
    model: str | None = None
    temperature: float = 0.0
    max_tokens: int = 4096
    timeout_s: float = 600.0
    max_tool_rounds: int = 8


#: The orchestrator answers after thinking, so it needs more room than a worker
#: whose output is a tool call.
ORCHESTRATOR_MAX_TOKENS = 8192

#: Share of the context window reserved for the reply when nothing explicit is
#: configured. A flat 4096 is far too little for an agent writing a whole file,
#: and the failure is ugly rather than graceful: the model stops mid-string and
#: the endpoint rejects the half-written tool call. An eighth scales with the
#: model — 8k on a 64k local window, 25k on Haiku's 200k.
OUTPUT_SHARE = 8
#: Above this, more output room buys nothing and costs input. No current model
#: will emit more than this in one reply anyway.
MAX_OUTPUT_TOKENS = 32_768


def default_output_tokens(window: int, floor: int) -> int:
    """Output budget for a window when no preset or role sets one explicitly."""
    return max(floor, min(MAX_OUTPUT_TOKENS, max(1024, window // OUTPUT_SHARE)))


@dataclass
class Config:
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    #: Named (provider, model) pairs — what an agent actually picks.
    presets: dict[str, ModelPreset] = field(default_factory=dict)
    #: Defaults for working agents (backend, frontend, tester, ...).
    worker: AgentDefaults = field(default_factory=AgentDefaults)
    #: The orchestrator is configured here, in main settings, not per-role.
    orchestrator: AgentDefaults = field(
        default_factory=lambda: AgentDefaults(max_tokens=ORCHESTRATOR_MAX_TOKENS))
    curator: CuratorSettings = field(default_factory=CuratorSettings)
    max_recurate_rounds: int = 2
    #: Steps estimated above this are broken up before the flow is proposed.
    #: 0 turns splitting off and leaves the estimates as information only.
    max_step_points: int = 5
    #: Ask the user before a refused write or command becomes final. Off means
    #: refuse outright, which is what an unattended run wants.
    ask_on_refusal: bool = True
    #: How long a blocked agent waits for an answer before the refusal stands.
    approval_timeout_s: float = 300.0
    #: A model preset used for one last attempt when a block has exhausted its
    #: loops. Empty = no escalation, halt as before. A block that has failed the
    #: same way three times is not going to be fixed by a fourth identical try;
    #: the one thing that has not been varied is the model.
    escalation_preset: str = ""
    #: Optional agent for that attempt. Empty = the step's own role, with the
    #: stronger model.
    escalation_role: str = ""
    runs_dir: str = "runs"
    #: Root for new projects. Empty means "the directory trance was started in".
    workspace: str = ""

    @property
    def workspace_root(self) -> Path:
        return Path(self.workspace).expanduser().resolve() if self.workspace else Path.cwd()

    # ------------------------------------------------------------- resolve

    def provider(self, name: str | None) -> ProviderConfig:
        if name and name in self.providers:
            return self.providers[name]
        if self.providers:
            return next(iter(self.providers.values()))
        return ProviderConfig()

    def resolve(self, defaults: AgentDefaults, *, provider: str | None = None,
                model: str | None = None, temperature: float | None = None,
                max_tokens: int | None = None, preset: str | None = None) -> ModelConfig:
        """Merge preset -> provider -> defaults -> overrides into one ModelConfig.

        A preset is the normal path: it names both the provider and the model,
        so an agent picks one thing. The separate provider/model arguments
        remain for configs written before presets existed.
        """
        chosen_preset = self.presets.get(preset or "") if preset else None
        if chosen_preset is not None:
            provider = chosen_preset.provider
            model = model or chosen_preset.model
            max_tokens = max_tokens or (chosen_preset.max_tokens or None)
        # A model that carries its own endpoint is its own provider. Falling
        # through to a named one would send it to whatever that URL happens to
        # be, which is how an Anthropic model ended up pointed at localhost.
        if chosen_preset is not None and chosen_preset.self_contained:
            chosen = chosen_preset.as_provider()
        else:
            chosen = self.provider(provider or defaults.provider)
        window = (chosen_preset.context_window if chosen_preset else 0) or chosen.context_window
        return ModelConfig(
            base_url=chosen.base_url,
            model=model or defaults.model or chosen.model,
            api_key=chosen.api_key,
            temperature=defaults.temperature if temperature is None else temperature,
            # An explicit setting on the preset or the role always wins; the
            # fallback scales with the window rather than sitting at 4096.
            max_tokens=max_tokens or default_output_tokens(window, defaults.max_tokens),
            timeout_s=defaults.timeout_s,
            max_tool_rounds=defaults.max_tool_rounds,
            context_window=window,
            provider=chosen.name,
            kind=chosen.kind,
        )

    def for_role(self, role) -> ModelConfig:
        """Resolved model settings for an AgentRole."""
        return self.resolve(
            self.worker,
            preset=getattr(role, "preset", None),
            provider=getattr(role, "provider", None),
            model=getattr(role, "model", None),
            temperature=getattr(role, "temperature", None),
            max_tokens=getattr(role, "max_tokens", None),
        )

    def for_orchestrator(self) -> ModelConfig:
        return self.resolve(self.orchestrator, preset=self.orchestrator.preset)

    # ---------------------------------------------------------------- load

    @classmethod
    def load(cls, path: Path | None = None, overrides: dict | None = None) -> "Config":
        cfg = cls()
        data = _read_toml(path)

        for name, raw in (data.get("providers") or {}).items():
            # Build in one shot: __post_init__ fills base_url/context_window
            # from `kind`, so setting kind afterwards would leave the previous
            # kind's endpoint in place — an Anthropic provider pointed at
            # localhost:12345.
            cfg.providers[name] = ProviderConfig.from_dict({**raw, "name": name})
        if not cfg.providers:
            cfg.providers["default"] = ProviderConfig()

        _apply(cfg.worker, data.get("worker", {}))
        _apply(cfg.orchestrator, data.get("orchestrator", {}))
        _apply(cfg.curator, data.get("curator", {}))
        _apply(cfg, {k: v for k, v in data.items()
                     if k not in ("worker", "orchestrator", "curator", "providers")})

        # A bare TRANCE_BASE_URL/TRANCE_MODEL retargets the default provider —
        # the common "just point it somewhere else" case.
        default = cfg.provider(cfg.worker.provider)
        for env_name, attr, cast in (
            ("TRANCE_BASE_URL", "base_url", str),
            ("TRANCE_MODEL", "model", str),
            ("TRANCE_API_KEY", "api_key", str),
            ("TRANCE_CONTEXT_WINDOW", "context_window", int),
        ):
            raw_value = os.environ.get(env_name)
            if raw_value:
                setattr(default, attr, cast(raw_value))
        if os.environ.get("TRANCE_WORKSPACE"):
            cfg.workspace = os.environ["TRANCE_WORKSPACE"]
        if os.environ.get("TRANCE_RUNS_DIR"):
            cfg.runs_dir = os.environ["TRANCE_RUNS_DIR"]

        for env_name, (section, attr, cast) in {
            "TRANCE_MAX_HOPS": ("curator", "max_hops", int),
            "TRANCE_TOKEN_BUDGET": ("curator", "token_budget", int),
        }.items():
            raw_value = os.environ.get(env_name)
            if raw_value:
                setattr(getattr(cfg, section), attr, cast(raw_value))

        for dotted, value in (overrides or {}).items():
            if value is None:
                continue
            section, _, attr = dotted.partition(".")
            if section == "provider":
                setattr(default, attr, value)
                continue
            target = getattr(cfg, section) if attr else cfg
            setattr(target, attr or section, value)
        return cfg

    def to_dict(self) -> dict:
        return {
            "providers": {n: p.to_dict() for n, p in self.providers.items()},
            "presets": {n: m.to_dict() for n, m in self.presets.items()},
            "worker": asdict(self.worker),
            "orchestrator": asdict(self.orchestrator),
            "curator": asdict(self.curator),
            "max_recurate_rounds": self.max_recurate_rounds,
            "max_step_points": self.max_step_points,
            "ask_on_refusal": self.ask_on_refusal,
            "escalation_preset": self.escalation_preset,
            "escalation_role": self.escalation_role,
            "approval_timeout_s": self.approval_timeout_s,
            "runs_dir": self.runs_dir,
            "workspace": str(self.workspace_root),
        }


def _read_toml(path: Path | None) -> dict:
    if path is None:
        for candidate in (Path.cwd() / CONFIG_FILENAME,
                          Path(__file__).resolve().parents[2] / CONFIG_FILENAME):
            if candidate.exists():
                path = candidate
                break
    if path is None or not Path(path).exists():
        return {}
    return tomllib.loads(Path(path).read_text(encoding="utf8"))


def _apply(obj, data: dict) -> None:
    known = {f.name for f in fields(obj)}
    for key, value in data.items():
        if key in known:
            setattr(obj, key, value)
