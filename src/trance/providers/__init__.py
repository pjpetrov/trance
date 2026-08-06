"""Provider registry: definitions, persistence, and client construction."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .base import (
    Cancelled,
    abort_inflight,
    KIND_DEFAULTS,
    BackendError,
    ChatResponse,
    ModelPreset,
    ProviderConfig,
    ProviderKind,
    ToolCall,
)

__all__ = [
    "ProviderConfig", "ProviderKind", "ChatResponse", "ToolCall", "BackendError",
    "KIND_DEFAULTS", "ModelPreset", "ProviderStore", "client_for", "Cancelled",
    "abort_inflight",
]


def client_for(config):
    """Build the client for a resolved ModelConfig, keyed on its provider kind.

    Anthropic speaks its own protocol (different endpoint, auth header, tool
    schema, and tool-result shape); everything else is OpenAI-compatible.
    """
    if getattr(config, "kind", "") == "anthropic":
        from .anthropic_client import AnthropicClient

        return AnthropicClient(config)

    from ..worker.client import ChatClient

    return ChatClient(config)


class ProviderStore:
    """Providers defined in the UI, persisted as JSON.

    `trance.toml` seeds the store on first run; after that the JSON file is the
    source of truth so edits made in the UI survive a restart.
    """

    def __init__(self, path: Path, seed: dict[str, ProviderConfig] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._providers: dict[str, ProviderConfig] = {}
        self._presets: dict[str, ModelPreset] = {}

        if self.path.exists():
            self._load()
        elif seed:
            self._providers = dict(seed)
            self._save()

    # ---------------------------------------------------------------- io

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf8"))
        except (OSError, json.JSONDecodeError):
            return
        for item in data.get("providers", []):
            try:
                provider = ProviderConfig.from_dict(item)
            except TypeError:
                continue
            self._providers[provider.name] = provider
        for item in data.get("presets", []):
            try:
                preset = ModelPreset.from_dict(item)
            except TypeError:
                continue
            self._presets[preset.name] = preset

    def _save(self) -> None:
        payload = {
            "providers": [p.to_dict(redact=False) for p in self._providers.values()],
            "presets": [m.to_dict() for m in self._presets.values()],
        }
        for entry in payload["providers"]:
            entry.pop("has_key", None)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf8")
        tmp.replace(self.path)  # atomic: never leave a half-written registry

    # -------------------------------------------------------------- api

    def all(self, enabled_only: bool = False) -> list[ProviderConfig]:
        items = list(self._providers.values())
        if enabled_only:
            items = [p for p in items if p.enabled]
        return sorted(items, key=lambda p: p.name)

    def get(self, name: str | None) -> ProviderConfig | None:
        return self._providers.get(name) if name else None

    def first(self) -> ProviderConfig | None:
        active = self.all(enabled_only=True)
        return active[0] if active else (self.all()[0] if self._providers else None)

    def upsert(self, provider: ProviderConfig) -> ProviderConfig:
        with self._lock:
            existing = self._providers.get(provider.name)
            # A blank key on update means "unchanged", not "clear it" — the UI
            # only ever sees the redacted placeholder.
            if existing is not None and not provider.api_key:
                provider.api_key = existing.api_key
            self._providers[provider.name] = provider
            self._save()
        return provider

    def delete(self, name: str) -> bool:
        with self._lock:
            removed = self._providers.pop(name, None) is not None
            if removed:
                self._save()
        return removed

    # ----------------------------------------------------------- presets

    def presets(self) -> list[ModelPreset]:
        """Only presets whose provider still exists and is enabled."""
        live = {p.name for p in self.all(enabled_only=True)}
        return sorted(
            (m for m in self._presets.values() if m.provider in live),
            key=lambda m: m.name,
        )

    def all_presets(self) -> list[ModelPreset]:
        return sorted(self._presets.values(), key=lambda m: m.name)

    def preset(self, name: str | None) -> ModelPreset | None:
        return self._presets.get(name) if name else None

    def upsert_preset(self, preset: ModelPreset) -> ModelPreset:
        with self._lock:
            self._presets[preset.name] = preset
            self._save()
        return preset

    def rename_preset(self, old: str, new: str) -> ModelPreset | None:
        """Rename a preset. Callers must re-point references themselves —
        the store has no view of who is using it."""
        with self._lock:
            if old == new or new in self._presets:
                return None
            preset = self._presets.pop(old, None)
            if preset is None:
                return None
            preset.name = new
            self._presets[new] = preset
            self._save()
        return preset

    def delete_preset(self, name: str) -> bool:
        with self._lock:
            removed = self._presets.pop(name, None) is not None
            if removed:
                self._save()
        return removed

    def seed_presets_from_providers(self) -> None:
        """Give every provider a starter preset so the picker is never empty."""
        if self._presets:
            return
        for provider in self.all():
            if provider.model:
                self._presets[provider.name] = ModelPreset(
                    name=provider.name, provider=provider.name, model=provider.model,
                    description=f"default model for {provider.name}",
                )
        if self._presets:
            self._save()

    def rename(self, old: str, new: str) -> bool:
        with self._lock:
            provider = self._providers.pop(old, None)
            if provider is None:
                return False
            provider.name = new
            self._providers[new] = provider
            self._save()
        return True
