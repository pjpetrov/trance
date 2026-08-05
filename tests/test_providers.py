"""Provider registry and the Anthropic protocol adapter.

The adapter tests matter most: Anthropic's wire format differs from the
OpenAI-compatible one in four ways, and a silent mistranslation shows up as a
confusing 400 mid-run rather than a clean failure here.
"""

import json

import pytest

from trance.config import Config
from trance.providers import KIND_DEFAULTS, ProviderConfig, ProviderStore, client_for
from trance.providers.anthropic_client import split_system, to_anthropic_tool


# --------------------------------------------------------------- registry

def test_kind_supplies_endpoint_defaults():
    provider = ProviderConfig(name="claude", kind="anthropic")
    assert provider.base_url == "https://api.anthropic.com"
    assert provider.model == "claude-opus-5"
    assert provider.context_window == 1_000_000

    local = ProviderConfig(name="lc", kind="llamacpp")
    assert local.base_url.startswith("http://localhost")


def test_explicit_values_beat_kind_defaults():
    provider = ProviderConfig(name="proxy", kind="anthropic",
                              base_url="https://gateway.internal/v1", model="claude-sonnet-5")
    assert provider.base_url == "https://gateway.internal/v1"
    assert provider.model == "claude-sonnet-5"


def test_store_roundtrips_and_persists(tmp_path):
    path = tmp_path / "providers.json"
    store = ProviderStore(path, seed={"llama": ProviderConfig(name="llama", kind="llamacpp")})
    store.upsert(ProviderConfig(name="claude", kind="anthropic", api_key="sk-ant-secret"))

    reopened = ProviderStore(path)
    assert {p.name for p in reopened.all()} == {"llama", "claude"}
    assert reopened.get("claude").api_key == "sk-ant-secret"


def test_blank_key_on_update_keeps_the_stored_one(tmp_path):
    """The UI only ever sees '***', so a blank key must mean 'unchanged'."""
    store = ProviderStore(tmp_path / "p.json")
    store.upsert(ProviderConfig(name="claude", kind="anthropic", api_key="sk-ant-secret"))
    store.upsert(ProviderConfig(name="claude", kind="anthropic", model="claude-sonnet-5"))
    assert store.get("claude").api_key == "sk-ant-secret"
    assert store.get("claude").model == "claude-sonnet-5"


def test_redacted_placeholder_is_never_stored():
    provider = ProviderConfig.from_dict({"name": "x", "kind": "anthropic", "api_key": "***"})
    assert provider.api_key is None


def test_keys_are_redacted_for_the_ui():
    data = ProviderConfig(name="x", kind="anthropic", api_key="sk-ant-secret").to_dict()
    assert data["api_key"] == "***" and data["has_key"] is True
    assert "sk-ant-secret" not in json.dumps(data)


def test_disabled_providers_are_hidden_from_pickers(tmp_path):
    store = ProviderStore(tmp_path / "p.json")
    store.upsert(ProviderConfig(name="on", kind="ollama"))
    store.upsert(ProviderConfig(name="off", kind="ollama", enabled=False))
    assert [p.name for p in store.all(enabled_only=True)] == ["on"]
    assert len(store.all()) == 2


def test_kind_is_carried_into_the_resolved_config(tmp_path):
    cfg_file = tmp_path / "trance.toml"
    cfg_file.write_text(
        '[providers.claude]\nkind = "anthropic"\nmodel = "claude-opus-5"\n'
        '\n[worker]\nprovider = "claude"\n'
    )
    config = Config.load(cfg_file)
    resolved = config.resolve(config.worker)
    assert resolved.kind == "anthropic" and resolved.model == "claude-opus-5"


def test_client_factory_dispatches_on_kind(tmp_path):
    cfg = Config.load(tmp_path / "none.toml")
    openai_like = cfg.resolve(cfg.worker)
    assert type(client_for(openai_like)).__name__ == "ChatClient"

    anthropic_like = cfg.resolve(cfg.worker)
    anthropic_like.kind = "anthropic"
    anthropic_like.api_key = "sk-ant-test"
    assert type(client_for(anthropic_like)).__name__ == "AnthropicClient"


def test_every_kind_has_defaults():
    for kind in ("anthropic", "openai", "ollama", "llamacpp"):
        assert {"base_url", "context_window", "label"} <= set(KIND_DEFAULTS[kind])


# ------------------------------------------------------- anthropic adapter

def test_system_messages_are_hoisted_out_of_the_message_list():
    system, messages = split_system([
        {"role": "system", "content": "You are precise."},
        {"role": "user", "content": "hello"},
    ])
    assert system == "You are precise."
    assert messages == [{"role": "user", "content": "hello"}]


def test_multiple_system_messages_merge():
    system, messages = split_system([
        {"role": "system", "content": "first"},
        {"role": "system", "content": "second"},
        {"role": "user", "content": "go"},
    ])
    assert system == "first\n\nsecond"
    assert len(messages) == 1


def test_tool_results_become_user_tool_result_blocks():
    """OpenAI's role='tool' message has no Anthropic equivalent."""
    _, messages = split_system([
        {"role": "user", "content": "read it"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "file contents"},
    ])
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"][0] == {
        "type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "a.py"}}
    assert messages[2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "call_1", "content": "file contents"}]}


def test_parallel_tool_results_merge_into_one_user_turn():
    """The API expects all results for one assistant turn in a single message."""
    _, messages = split_system([
        {"role": "tool", "tool_call_id": "a", "content": "one"},
        {"role": "tool", "tool_call_id": "b", "content": "two"},
    ])
    assert len(messages) == 1
    assert [b["tool_use_id"] for b in messages[0]["content"]] == ["a", "b"]


def test_parsed_assistant_blocks_round_trip_verbatim():
    """tool_result must match a tool_use from the same assistant message."""
    blocks = [{"type": "tool_use", "id": "toolu_1", "name": "x", "input": {}}]
    _, messages = split_system([
        {"role": "assistant", "content": "", "_anthropic_content": blocks},
    ])
    assert messages[0]["content"] is blocks


def test_tool_schema_is_flattened():
    """Anthropic uses {name, description, input_schema}, not a `function` wrapper."""
    converted = to_anthropic_tool({
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write a file.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    })
    assert converted == {
        "name": "write_file",
        "description": "Write a file.",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    }
    assert "function" not in converted


def test_tool_schema_conversion_is_idempotent():
    already = {"name": "x", "description": "d", "input_schema": {"type": "object"}}
    assert to_anthropic_tool(already) == already


def test_sampling_params_are_never_sent(monkeypatch, tmp_path):
    """temperature/top_p/top_k return a 400 on current Claude models."""
    captured = {}

    class FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop here — we only care about the payload")

    class FakeAnthropic:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    cfg = Config.load(tmp_path / "none.toml")
    resolved = cfg.resolve(cfg.worker)
    resolved.kind, resolved.api_key, resolved.temperature = "anthropic", "sk-ant-test", 0.7

    with pytest.raises(RuntimeError):
        client_for(resolved).complete([{"role": "user", "content": "hi"}])

    assert "temperature" not in captured
    assert "top_p" not in captured and "top_k" not in captured
    assert captured["max_tokens"] > 0  # required by the API


# --------------------------------------------------------------- presets

def test_preset_supplies_both_provider_and_model(tmp_path):
    """The whole point: an agent picks one thing, not two."""
    from trance.providers import ModelPreset
    from trance.agents.roles import AgentRole

    cfg_file = tmp_path / "trance.toml"
    cfg_file.write_text(
        '[providers.claude]\nkind = "anthropic"\nmodel = "claude-opus-5"\n'
        '[providers.local]\nkind = "ollama"\nmodel = "qwen2.5-coder:32b"\n'
        '\n[worker]\nprovider = "local"\n'
    )
    config = Config.load(cfg_file)
    config.presets = {
        "smart": ModelPreset(name="smart", provider="claude", model="claude-sonnet-5"),
    }
    role = AgentRole(name="tester", title="T", description="", system_prompt="", preset="smart")
    resolved = config.for_role(role)

    assert resolved.provider == "claude"          # from the preset
    assert resolved.model == "claude-sonnet-5"    # from the preset, not the provider default
    assert resolved.kind == "anthropic"
    assert resolved.context_window == 1_000_000   # inherited from the provider


def test_preset_can_override_the_context_window(tmp_path):
    from trance.providers import ModelPreset

    cfg = Config.load(tmp_path / "none.toml")
    cfg.providers = {"p": ProviderConfig(name="p", kind="ollama")}
    cfg.presets = {"m": ModelPreset(name="m", provider="p", model="x", context_window=8000)}
    assert cfg.resolve(cfg.worker, preset="m").context_window == 8000


def test_role_without_a_preset_still_resolves(tmp_path):
    """Configs written before presets existed keep working."""
    from trance.agents.roles import AgentRole

    cfg_file = tmp_path / "trance.toml"
    cfg_file.write_text('[providers.local]\nkind = "ollama"\nmodel = "m1"\n\n[worker]\nprovider = "local"\n')
    config = Config.load(cfg_file)
    role = AgentRole(name="x", title="X", description="", system_prompt="")
    assert config.for_role(role).model == "m1"


def test_presets_hide_when_their_provider_is_disabled(tmp_path):
    from trance.providers import ModelPreset

    store = ProviderStore(tmp_path / "p.json")
    store.upsert(ProviderConfig(name="local", kind="ollama"))
    store.upsert_preset(ModelPreset(name="fast", provider="local", model="m"))
    assert [m.name for m in store.presets()] == ["fast"]

    store.upsert(ProviderConfig(name="local", kind="ollama", enabled=False))
    assert store.presets() == []          # not offered to agents
    assert len(store.all_presets()) == 1  # but still configured


def test_seeding_gives_every_provider_a_starter_model(tmp_path):
    store = ProviderStore(tmp_path / "p.json", seed={
        "llama": ProviderConfig(name="llama", kind="llamacpp", model="qwen"),
    })
    store.seed_presets_from_providers()
    assert [(m.name, m.model) for m in store.all_presets()] == [("llama", "qwen")]

    store.seed_presets_from_providers()  # idempotent — never clobbers edits
    assert len(store.all_presets()) == 1


def test_presets_persist(tmp_path):
    from trance.providers import ModelPreset

    path = tmp_path / "p.json"
    store = ProviderStore(path)
    store.upsert(ProviderConfig(name="claude", kind="anthropic", api_key="k"))
    store.upsert_preset(ModelPreset(name="opus", provider="claude", model="claude-opus-5"))
    assert [m.model for m in ProviderStore(path).all_presets()] == ["claude-opus-5"]


def test_rename_preset_moves_it(tmp_path):
    from trance.providers import ModelPreset

    store = ProviderStore(tmp_path / "p.json")
    store.upsert(ProviderConfig(name="claude", kind="anthropic"))
    store.upsert_preset(ModelPreset(name="m1", provider="claude", model="claude-opus-5"))

    renamed = store.rename_preset("m1", "smart")
    assert renamed.name == "smart" and renamed.model == "claude-opus-5"
    assert store.preset("m1") is None
    assert [m.name for m in ProviderStore(tmp_path / "p.json").all_presets()] == ["smart"]


def test_rename_refuses_to_clobber_an_existing_name(tmp_path):
    from trance.providers import ModelPreset

    store = ProviderStore(tmp_path / "p.json")
    store.upsert(ProviderConfig(name="claude", kind="anthropic"))
    store.upsert_preset(ModelPreset(name="a", provider="claude", model="x"))
    store.upsert_preset(ModelPreset(name="b", provider="claude", model="y"))

    assert store.rename_preset("a", "b") is None
    assert {m.name for m in store.all_presets()} == {"a", "b"}
    assert store.preset("b").model == "y"  # untouched


def test_rename_to_the_same_name_is_a_noop(tmp_path):
    from trance.providers import ModelPreset

    store = ProviderStore(tmp_path / "p.json")
    store.upsert(ProviderConfig(name="p", kind="ollama"))
    store.upsert_preset(ModelPreset(name="a", provider="p", model="x"))
    assert store.rename_preset("a", "a") is None
    assert store.preset("a") is not None


def test_rename_unknown_preset_returns_none(tmp_path):
    store = ProviderStore(tmp_path / "p.json")
    assert store.rename_preset("nope", "other") is None
