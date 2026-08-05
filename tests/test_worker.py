"""Worker tests. The LLM is stubbed — these cover the loop, the tools, and the
response parsing, which is where the bugs actually live."""

from pathlib import Path

import pytest

from trance.config import Config, ModelConfig
from trance.curator.walker import CuratorConfig, curate
from trance.db import GraphDB
from trance.indexer.service import index_repo
from trance.worker import agent
from trance.worker.client import ChatResponse, ToolCall, _parse
from trance.worker.tools import ContextTools

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "sample-app"


@pytest.fixture
def db(tmp_path):
    db = GraphDB(tmp_path / "graph.db")
    index_repo(SAMPLE, db)
    yield db
    db.close()


@pytest.fixture
def tools(db):
    return ContextTools(db, SAMPLE)


# ------------------------------------------------------------------- tools

def test_get_definition_returns_source(tools):
    result = tools.get_definition("format_currency")
    assert result.hit and "cents / 100" in result.text


def test_get_definition_on_a_file_returns_an_outline_not_a_random_symbol(tools):
    """Regression: substring matching used to return one arbitrary symbol and
    report it as a hit, which reads as a confident wrong answer."""
    result = tools.get_definition("backend/app/services.py")
    assert result.hit
    assert "is a file, not a symbol" in result.text
    assert "list_for_user" in result.text and "PAGE_SIZE" in result.text


def test_module_constants_are_indexed(tools):
    result = tools.get_definition("PAGE_SIZE")
    assert result.hit and "25" in result.text


def test_missing_symbol_is_a_miss_with_guidance(tools):
    result = tools.get_definition("no_such_symbol")
    assert not result.hit and "search_symbols" in result.text


def test_callers_and_callees(tools):
    assert "serialize_order" in tools.get_callers("format_currency").text
    assert "format_currency" in tools.get_callees("serialize_order").text


def test_unknown_tool_and_bad_arguments_do_not_crash(tools):
    assert not tools.call("nope", {}).hit
    assert not tools.call("get_definition", {"wrong": "arg"}).hit


# ------------------------------------------------------------------ client

def test_parse_extracts_tool_calls_and_reasoning():
    resp = _parse({
        "choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": "", "reasoning_content": "thinking…",
            "tool_calls": [{"id": "1", "type": "function", "function": {
                "name": "get_definition", "arguments": '{"symbol": "foo"}'}}]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    })
    assert resp.tool_calls[0].name == "get_definition"
    assert resp.tool_calls[0].arguments == {"symbol": "foo"}
    assert resp.reasoning == "thinking…"


def test_malformed_tool_arguments_do_not_crash():
    resp = _parse({"choices": [{"message": {"tool_calls": [
        {"id": "1", "function": {"name": "x", "arguments": "{not json"}}]}}]})
    assert resp.tool_calls[0].arguments == {}


# ------------------------------------------------------------------- agent

class FakeClient:
    """Replays a scripted sequence of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, tools=None):
        self.calls.append({"messages": list(messages), "tools": tools})
        return self.responses.pop(0)


def _run(monkeypatch, db, responses):
    fake = FakeClient(responses)
    monkeypatch.setattr(agent, "ChatClient", lambda config: fake)
    bundle = curate(db, SAMPLE, "t", "get_user_orders", CuratorConfig(max_hops=1))
    result = agent.run(bundle, db, SAMPLE, ModelConfig(max_tool_rounds=3))
    return result, fake


def test_tool_loop_feeds_results_back(monkeypatch, db):
    result, fake = _run(monkeypatch, db, [
        ChatResponse(text="", tool_calls=[ToolCall("c1", "get_definition", {"symbol": "PAGE_SIZE"})],
                     finish_reason="tool_calls", raw_message={"role": "assistant", "tool_calls": []}),
        ChatResponse(text="done\n```diff\n--- a/x\n+++ b/x\n```", finish_reason="stop"),
    ])
    assert len(result.tool_calls) == 1 and result.tool_calls[0].hit
    tool_messages = [m for m in fake.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_messages and "25" in tool_messages[0]["content"]
    assert result.diff is not None


def test_need_context_is_parsed(monkeypatch, db):
    result, _ = _run(monkeypatch, db, [
        ChatResponse(text="NEED_CONTEXT: OrderService.create, validate_payload", finish_reason="stop"),
    ])
    assert result.needs_more_context
    assert result.requested_context == ["OrderService.create", "validate_payload"]


def test_exhausting_tool_rounds_still_produces_an_answer(monkeypatch, db):
    """Regression: the loop used to return empty text after burning its budget."""
    looping = ChatResponse(
        text="", tool_calls=[ToolCall("c", "search_symbols", {"pattern": "zzz"})],
        finish_reason="tool_calls", raw_message={"role": "assistant", "tool_calls": []},
    )
    result, fake = _run(monkeypatch, db, [looping, looping, looping,
                                          ChatResponse(text="final answer", finish_reason="stop")])
    assert result.stop_reason == "max_tool_rounds"
    assert result.text == "final answer"
    assert fake.calls[-1]["tools"] is None  # tools withheld on the forced turn


# ------------------------------------------------------------------ config

def test_config_precedence(tmp_path, monkeypatch):
    """file <- environment <- CLI flags, against the provider-based shape."""
    cfg_file = tmp_path / "trance.toml"
    cfg_file.write_text(
        '[providers.local]\nbase_url = "http://from-file/v1"\nmodel = "file-model"\n'
        '\n[worker]\nprovider = "local"\n'
    )
    assert Config.load(cfg_file).resolve(Config.load(cfg_file).worker).base_url == "http://from-file/v1"

    # A bare TRANCE_BASE_URL retargets the provider the worker actually uses.
    monkeypatch.setenv("TRANCE_BASE_URL", "http://from-env/v1")
    env_cfg = Config.load(cfg_file)
    assert env_cfg.resolve(env_cfg.worker).base_url == "http://from-env/v1"

    flagged = Config.load(cfg_file, overrides={"provider.base_url": "http://from-flag/v1"})
    resolved = flagged.resolve(flagged.worker)
    assert resolved.base_url == "http://from-flag/v1"
    assert resolved.model == "file-model"  # unset flags don't clobber


def test_api_key_never_reaches_a_trace(tmp_path):
    cfg_file = tmp_path / "trance.toml"
    cfg_file.write_text('[providers.p]\napi_key = "secret-key"\n')
    assert "secret-key" not in str(Config.load(cfg_file).to_dict())
