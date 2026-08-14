"""Worker tests. The LLM is stubbed — these cover the loop, the tools, and the
response parsing, which is where the bugs actually live."""

import time
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

    def complete(self, messages, tools=None, **kwargs):
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


# ------------------------------- stopping a generation that is already running

def _slow_model_server(delay_s: float):
    """An endpoint that accepts the request and then thinks for a long time."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            time.sleep(delay_s)                      # generating…
            try:
                self.wfile.write(b'{"choices":[{"message":{"content":"done"}}]}')
            except OSError:
                pass

        def log_message(self, *args):
            pass

    # Threaded, or shutdown() would block until the sleeping handler returns —
    # the test would then take exactly as long as the hang it is proving we can
    # break off.
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_stop_breaks_off_a_generation_in_flight():
    """Regression: stop was only read between rounds, so pressing it did
    nothing visible until the model finished — minutes, on a local one."""
    import threading

    from trance.config import ModelConfig
    from trance.providers.base import Cancelled, abort_inflight
    from trance.worker.client import ChatClient

    server = _slow_model_server(20)
    port = server.server_address[1]
    client = ChatClient(ModelConfig(base_url=f"http://127.0.0.1:{port}/v1", timeout_s=60))
    result = {}

    def call():
        try:
            client.complete([{"role": "user", "content": "hi"}], cancel_token="s1")
        except Cancelled:
            result["cancelled"] = True
        except Exception as exc:                     # noqa: BLE001
            result["error"] = exc

    started = time.time()
    worker = threading.Thread(target=call)
    worker.start()
    time.sleep(0.6)                                  # let it get into read()

    assert abort_inflight("s1") == 1
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert result.get("cancelled") is True, result
    assert time.time() - started < 5                 # not the full 20s
    server.shutdown()


def test_a_finished_call_is_not_left_registered():
    """A stale handle would make the next stop think it aborted something."""
    from trance.config import ModelConfig
    from trance.providers.base import _INFLIGHT, abort_inflight
    from trance.worker.client import ChatClient

    server = _slow_model_server(0)
    port = server.server_address[1]
    client = ChatClient(ModelConfig(base_url=f"http://127.0.0.1:{port}/v1", timeout_s=10))
    client.complete([{"role": "user", "content": "hi"}], cancel_token="s2")

    assert "s2" not in _INFLIGHT
    assert abort_inflight("s2") == 0
    server.shutdown()


def test_aborting_an_unknown_session_is_harmless():
    from trance.providers.base import abort_inflight

    assert abort_inflight("never-existed") == 0
    assert abort_inflight("") == 0


def test_every_request_identifies_itself(monkeypatch):
    """urllib's default agent is "Python-urllib/3.x", which Cloudflare refuses
    outright — a working curl and a failing trance against the same URL,
    answered with "error code: 1010"."""
    import io
    import json
    import urllib.request

    from trance.config import ModelConfig
    from trance.worker.client import USER_AGENT, ChatClient

    seen = {}

    def capture(request, timeout=None):
        seen["headers"] = dict(request.header_items())
        body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        return io.BytesIO(body).__enter__() if False else _Resp(body)

    class _Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", capture)
    ChatClient(ModelConfig()).complete([{"role": "user", "content": "hi"}])

    agent = {k.lower(): v for k, v in seen["headers"].items()}["user-agent"]
    assert agent == USER_AGENT and "trance" in agent
    assert "urllib" not in agent


def test_model_discovery_identifies_itself_too(monkeypatch):
    import json
    import urllib.request

    from trance.providers.base import list_models
    from trance.worker.client import USER_AGENT

    seen = {}

    class _Resp:
        def read(self):
            return json.dumps({"data": [{"id": "a"}]}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def capture(request, timeout=None):
        seen["headers"] = {k.lower(): v for k, v in request.header_items()}
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", capture)
    list_models("openai", "https://example/v1", "sk-x")
    assert seen["headers"]["user-agent"] == USER_AGENT
    assert seen["headers"]["authorization"] == "Bearer sk-x"


def _sse(lines):
    """A fake streamed response: the header says event-stream, iteration
    yields SSE frames, and close() is what urlopen's context manager calls."""
    import email.message

    headers = email.message.Message()
    headers["Content-Type"] = "text/event-stream"

    class _Stream:
        def __init__(self):
            self.headers = headers

        def __iter__(self):
            return iter([line.encode() for line in lines])

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Stream()


def test_streamed_chunks_reassemble_into_one_response(monkeypatch):
    """Streaming changes when we see the reply, not what anyone receives:
    reasoning, text, tool calls and usage come out exactly as they would have
    from a whole response, and the request asks for the stream explicitly."""
    import json
    import urllib.request

    from trance.config import ModelConfig
    from trance.worker.client import ChatClient

    sent = {}

    def capture(request, timeout=None):
        sent["payload"] = json.loads(request.data)
        return _sse([
            'data: {"choices":[{"delta":{"reasoning_content":"hmm "}}]}\n',
            'data: {"choices":[{"delta":{"reasoning_content":"okay"}}]}\n',
            'data: {"choices":[{"delta":{"content":"Done"}}]}\n',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
            '"function":{"name":"list_files","arguments":"{\\"path\\""}}]}}]}\n',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
            '"function":{"arguments":":\\".\\"}"}}]}}]}\n',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n',
            'data: {"usage":{"prompt_tokens":10,"completion_tokens":6}}\n',
            "data: [DONE]\n",
        ])

    monkeypatch.setattr(urllib.request, "urlopen", capture)
    got = ChatClient(ModelConfig()).complete([{"role": "user", "content": "hi"}])

    assert sent["payload"]["stream"] is True
    assert sent["payload"]["stream_options"] == {"include_usage": True}
    assert got.reasoning == "hmm okay"
    assert got.text == "Done"
    assert got.finish_reason == "tool_calls"
    assert [(c.name, c.arguments) for c in got.tool_calls] == [("list_files", {"path": "."})]
    assert got.usage == {"prompt_tokens": 10, "completion_tokens": 6}
    # Replayed into the next request as-is — llama.cpp rejects the whole
    # conversation over a message without a role.
    assert got.raw_message["role"] == "assistant"


def test_a_generation_that_outlives_its_time_budget_is_cut_not_errored(monkeypatch):
    """The size limit was the wrong governor — a productive think is cut by
    wall clock instead. Whatever was generated up to the cut comes back, marked
    `finish_reason: "time"`, so the overrun machinery treats it exactly like a
    reply the server cut at max_tokens. Progress was reported while it ran."""
    import urllib.request

    from trance.config import ModelConfig
    from trance.worker.client import ChatClient

    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda request, timeout=None: _sse([
            'data: {"choices":[{"delta":{"reasoning_content":"thinking hard…"}}]}\n',
            'data: {"choices":[{"delta":{"reasoning_content":"never sent"}}]}\n',
        ]))

    frames = []
    got = ChatClient(ModelConfig(timeout_s=0)).complete(
        [{"role": "user", "content": "hi"}], on_progress=frames.append)

    assert got.finish_reason == "time"
    assert got.reasoning == "thinking hard…"        # the cut kept what was paid for
    assert got.text == ""
    assert frames and frames[0]["phase"] == "thinking"
    assert frames[0]["tokens"] == 1 and frames[0]["tail"].endswith("hard…")


def test_a_mid_stream_truncation_error_is_recovered_like_the_http_one(monkeypatch):
    """llama.cpp reports a tool call the model never finished as an in-stream
    error frame when streaming, where the non-streaming path got an HTTP 500.
    Same cause, same recovery: length-cut with `truncated_tool_call`."""
    import urllib.request

    from trance.config import ModelConfig
    from trance.worker.client import ChatClient

    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda request, timeout=None: _sse([
            'data: {"choices":[{"delta":{"content":"writing…"}}]}\n',
            'data: {"error":{"message":"Failed to parse tool call arguments: '
            'unexpected end of input"}}\n',
        ]))

    got = ChatClient(ModelConfig()).complete([{"role": "user", "content": "hi"}])
    assert got.finish_reason == "length"
    assert got.provider_error == "truncated_tool_call"
