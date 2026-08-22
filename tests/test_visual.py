"""The browser toolset: driving a real page, and judging a picture of it.

Two things are worth being careful about here and both have a test below. The
first is that absence of a browser is a normal state, not a failure — every
path has to degrade to a readable tool result rather than an exception that
kills a step. The second is that a vision model will answer confidently when it
has been shown nothing, so an empty or missing answer must never reach the agent
looking like an opinion.
"""

from __future__ import annotations

import pytest

from trance.agents.roles import BUILTIN_ROLES, TOOLSETS, AgentRole
from trance.agents.tools import AgentTools, permissions_brief
from trance.agents.visual import default_page
from trance.browser import find_chrome, key_spec
from trance.config import ModelConfig

needs_chrome = pytest.mark.skipif(find_chrome() is None,
                                  reason="no Chrome on this machine")


def _role(**kw) -> AgentRole:
    base = dict(name="looker", title="Looker", description="", system_prompt="",
                toolsets=["browser"], paths=[])
    return AgentRole(**{**base, **kw})


# ------------------------------------------------------------------ wiring

def test_browser_is_a_toolset_and_the_visual_tester_writes_nothing():
    assert "browser" in TOOLSETS
    role = BUILTIN_ROLES["visual-tester"]
    assert role.toolsets == ["browser", "inspect"]
    # It judges other agents' work, so it must not be able to do that work.
    assert role.paths == [] and role.may_write("src/game.js") is False
    assert role.verifier is True


def test_the_browser_tools_are_offered_only_to_agents_granted_them(tmp_path):
    with_browser = AgentTools(tmp_path, _role())
    names = {s["function"]["name"] for s in with_browser.specs()}
    assert {"open_page", "press_key", "check_canvas", "look"} <= names

    without = AgentTools(tmp_path, _role(toolsets=["files"], paths=["**"]))
    plain = {s["function"]["name"] for s in without.specs()}
    assert not plain & {"open_page", "press_key", "check_canvas", "look"}

    # And naming one anyway is refused rather than dispatched.
    refused = without.call("look", {"question": "how does it look?"})
    assert refused.ok is False and "do not have" in refused.text


def test_the_prompt_says_how_the_app_is_served():
    """The agent has to know what is behind the page it is judging: the
    project's own dev server when it needs one, static files otherwise."""
    brief = permissions_brief(_role())
    assert "headless browser" in brief
    assert "dev server" in brief and "package.json" in brief
    assert "statically" in brief


def test_looking_without_a_vision_model_is_a_readable_refusal(tmp_path):
    tools = AgentTools(tmp_path, _role(), vision_config=None)
    out = tools.call("look", {"question": "is it right?"})
    assert out.ok is False
    assert "no vision model" in out.detail["error"]
    # It must tell the agent what to do instead, or the step just stops here.
    assert "check_canvas" in out.text and "could not be made" in out.text


# ------------------------------------------------------------------- keys

@pytest.mark.parametrize("name, expected_code, expected_key_code", [
    ("Space", "Space", 32),
    ("space", "Space", 32),            # a model will not match our capitals
    ("ArrowLeft", "ArrowLeft", 37),
    ("w", "KeyW", 87),
    ("1", "Digit1", 49),
])
def test_key_names_map_to_what_a_game_listens_for(name, expected_code, expected_key_code):
    key, code, key_code, _text = key_spec(name)
    assert code == expected_code
    # keyCode matters: plenty of game input reads event.keyCode and a 0 there
    # means the press silently does nothing.
    assert key_code == expected_key_code


def test_an_unknown_key_says_what_is_available():
    with pytest.raises(ValueError) as exc:
        key_spec("PageUpTwice")
    assert "ArrowLeft" in str(exc.value)


# ------------------------------------------------------------- page finding

def test_the_page_is_found_where_projects_actually_put_it(tmp_path):
    assert default_page(tmp_path) == ""                     # nothing to open yet
    (tmp_path / "public").mkdir()
    (tmp_path / "public" / "index.html").write_text("<canvas></canvas>")
    assert default_page(tmp_path) == "public/index.html"
    (tmp_path / "index.html").write_text("<canvas></canvas>")
    assert default_page(tmp_path) == "index.html"           # the root one wins


def test_node_modules_is_never_offered_as_the_page(tmp_path):
    deep = tmp_path / "node_modules" / "pkg"
    deep.mkdir(parents=True)
    (deep / "index.html").write_text("<p>a dependency's own demo page</p>")
    assert default_page(tmp_path) == ""


# ----------------------------------------------------------- page errors

def test_page_errors_are_deduped_and_capped():
    from trance.agents.tools import MAX_PAGE_ERRORS, _render_page_errors

    same = ["Uncaught TypeError: x is not a function"] * 40
    rendered = _render_page_errors({"console": same, "exceptions": [], "failed_requests": []})
    # One broken import can log every frame; forty identical lines tell the
    # agent nothing the first one did and cost it context to read.
    assert rendered.count("Uncaught TypeError") == 1

    many = [f"error {n}" for n in range(MAX_PAGE_ERRORS + 5)]
    capped = _render_page_errors({"console": many, "exceptions": [], "failed_requests": []})
    assert "and 5 more" in capped

    assert "No console errors" in _render_page_errors({})


# ---------------------------------------------------------------- vision

class _Stub:
    """Stands in for a chat client, recording what it was sent."""

    def __init__(self, text="ok", finish_reason="stop"):
        self.text, self.finish_reason, self.sent, self.extra = text, finish_reason, None, None

    def complete(self, messages, tools=None, cancel_token="", extra_body=None):
        from trance.providers.base import ChatResponse

        self.sent, self.extra = messages, extra_body
        return ChatResponse(text=self.text, finish_reason=self.finish_reason,
                            usage={"prompt_tokens": 400, "completion_tokens": 20})


def _vision_config(kind="llamacpp", max_tokens=4096):
    return ModelConfig(base_url="http://localhost:1/v1", model="m", kind=kind,
                       preset="looker", max_tokens=max_tokens)


def test_the_image_rides_the_message_in_the_shape_each_api_speaks(monkeypatch):
    from trance import vision

    stub = _Stub(text="1. DESCRIBE ... 3. ANSWER fine")
    monkeypatch.setattr(vision, "client_for", lambda config: stub)
    result = vision.look(b"\x89PNG-not-really", "is it right?", _vision_config(),
                         checks=["the maze is drawn"])

    content = stub.sent[0]["content"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    # The question is asked in the shape that keeps the answer checkable.
    assert "DESCRIBE" in content[0]["text"] and "the maze is drawn" in content[0]["text"]
    assert result["answer"].endswith("fine") and result["preset"] == "looker"

    anthropic = _Stub(text="fine")
    monkeypatch.setattr(vision, "client_for", lambda config: anthropic)
    vision.look(b"png", "q", _vision_config(kind="anthropic"))
    assert anthropic.sent[0]["content"][1]["source"]["type"] == "base64"
    # Only backends known to accept it are told not to think.
    assert anthropic.extra is None


def test_a_reasoning_model_is_told_not_to_think_and_given_room_to_answer(monkeypatch):
    """Regression: measured against the local Qwen, the whole output budget went
    to reasoning and the answer came back empty with finish_reason=length."""
    from trance import vision

    stub = _Stub(text="an answer")
    captured = {}

    def factory(config):
        captured["max_tokens"] = config.max_tokens
        return stub

    monkeypatch.setattr(vision, "client_for", factory)
    vision.look(b"png", "q", _vision_config(max_tokens=120))
    assert stub.extra == {"chat_template_kwargs": {"enable_thinking": False}}
    assert captured["max_tokens"] >= vision.MIN_ANSWER_TOKENS


def test_an_empty_answer_is_never_passed_off_as_an_opinion(monkeypatch):
    """A model that said nothing has judged nothing, and "" reads as "no
    problems found" to everything downstream."""
    from trance import vision

    monkeypatch.setattr(vision, "client_for",
                        lambda config: _Stub(text="   ", finish_reason="length"))
    with pytest.raises(vision.VisionUnavailable) as exc:
        vision.look(b"png", "q", _vision_config())
    assert "empty answer" in str(exc.value)
    assert "raise max_tokens" in str(exc.value)          # says how to fix it


def test_a_model_that_cannot_see_says_so_rather_than_guessing():
    from trance import vision

    with pytest.raises(vision.VisionUnavailable) as exc:
        vision.look(b"png", "q", _vision_config(kind="claudecode"))
    assert "cannot be sent an image" in str(exc.value)


def test_a_failed_vision_call_still_keeps_the_screenshot(tmp_path, monkeypatch):
    """The picture is the one artefact that lets a person check the verdict, so
    losing it because the model errored is the worst of both outcomes."""
    from trance import vision as vision_module
    from trance.agents import tools as tools_module

    tools = AgentTools(tmp_path, _role(), step_id="st-9", vision_config=_vision_config())

    class _Session:
        def capture(self, whole_page=False):
            return b"png-bytes", {"clipped": True, "page": "index.html", "url": "u", "bytes": 9}

        def save(self, png):
            return "st-9/001.png"

    monkeypatch.setattr(type(tools), "visual", property(lambda self: _Session()))
    monkeypatch.setattr(vision_module, "client_for",
                        lambda config: _Stub(text="", finish_reason="stop"))

    out = tools.call("look", {"question": "anything?"})
    assert out.ok is False
    assert out.detail["kind"] == "screenshot"          # still renders as evidence
    assert out.detail["shot"] == "st-9/001.png"
    assert out.detail["answer"] == "" and out.detail["error"]
    assert tools_module is not None


# ----------------------------------------------------------- serving shots

def test_screenshots_are_served_from_the_session_and_nothing_else_is(tmp_path):
    from fastapi.testclient import TestClient

    from trance.agents.visual import SHOTS_DIR
    from trance.config import Config
    from trance.server import app as app_module

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    client = TestClient(app_module.create_app(config, tmp_path / "sessions"))
    project = tmp_path / "proj"
    sid = client.post("/api/sessions",
                      json={"name": "p", "project_dir": str(project)}).json()["id"]

    shots = project / SHOTS_DIR / "st-1"
    shots.mkdir(parents=True)
    (shots / "001.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (project / "secret.txt").write_text("not a screenshot")

    good = client.get(f"/api/sessions/{sid}/shot/st-1/001.png")
    assert good.status_code == 200
    assert good.headers["content-type"] == "image/png"
    assert good.content == b"\x89PNG\r\n\x1a\nfake"

    assert client.get(f"/api/sessions/{sid}/shot/st-1/nope.png").status_code == 404
    # The shot route must not become a way to read the project.
    assert client.get(f"/api/sessions/{sid}/shot/../../secret.txt").status_code == 404


def test_the_config_reports_whether_visual_checks_are_possible(tmp_path):
    from fastapi.testclient import TestClient

    from trance.config import Config
    from trance.server import app as app_module

    config = Config.load(tmp_path / "none.toml")
    config.runs_dir = str(tmp_path / "runs")
    client = TestClient(app_module.create_app(config, tmp_path / "sessions"))

    visual = client.get("/api/config").json()["visual"]
    # Whether a browser exists, and nothing else. There is deliberately no
    # vision-model setting: screenshots go to the model the agent already has,
    # so there is no second place to configure and no way for the two to
    # disagree about which model an agent is using.
    assert set(visual) == {"browser"}
    assert isinstance(visual["browser"], bool)
    assert "vision_preset" not in client.put("/api/config/planning", json={}).json()


def test_the_agents_own_model_is_what_gets_shown_the_screenshot(tmp_path, monkeypatch):
    """The `look` tool sends the image to whatever model the agent runs on, so
    an agent with the browser toolset needs one that can see."""
    from trance.agents import runner

    captured = {}

    class _Tools:
        def __init__(self, *a, **kw):
            captured.update(kw)

        def specs(self):
            raise RuntimeError("stop here — the wiring is all this test needs")

        def close(self):
            pass

    monkeypatch.setattr(runner, "AgentTools", _Tools)
    monkeypatch.setattr(runner, "client_for", lambda config: object())
    config = _vision_config()
    with pytest.raises(RuntimeError):
        runner.run_agent(role=_role(), task="t", project=tmp_path, config=config,
                         bus=__import__("trance.events", fromlist=["EventBus"]).EventBus(),
                         session_id="s", step_id="st")
    assert captured["vision_config"] is config


# ------------------------------------------------- the real thing, if we can

@needs_chrome
def test_a_real_browser_loads_a_page_and_reads_its_canvas_back(tmp_path):
    """The whole chain against a real Chrome: paint, measure, key, screenshot."""
    from trance.browser import Browser

    page = tmp_path / "index.html"
    page.write_text("""
      <canvas id="c" width="120" height="80"></canvas>
      <script>
        const ctx = document.getElementById('c').getContext('2d');
        let started = false, n = 0;
        addEventListener('keydown', e => { if (e.code === 'Space') started = true; });
        (function draw() {
          ctx.fillStyle = started ? '#c00' : '#000';
          ctx.fillRect(0, 0, 120, 80);
          if (started) { ctx.fillStyle = '#ff0'; ctx.fillRect((n += 2) % 100, 20, 10, 10); }
          requestAnimationFrame(draw);
        })();
      </script>
    """, encoding="utf8")

    from trance import preview

    served = preview.serve(tmp_path, host="127.0.0.1", port=0)
    try:
        with Browser() as browser:
            loaded = browser.navigate(f"http://127.0.0.1:{served.port}/index.html")
            assert loaded["frames"] == loaded["asked_frames"]   # the loop is running
            assert loaded["errors"]["total"] == 0

            before = browser.probe()
            assert before["canvas"] is True and before["w"] == 120
            # Painted one flat colour: exactly what a crashed game looks like,
            # and the reason this check exists at all.
            assert before["uniform"] is True

            browser.press("Space")
            browser.wait_frames(30)
            after = browser.probe()
            assert after["uniform"] is False                    # it drew something
            assert after["hash"] != before["hash"]

            png = browser.screenshot(browser.canvas_clip(after))
            assert png.startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        served.stop()


@needs_chrome
def test_a_dead_render_loop_shows_up_as_frozen(tmp_path):
    """The bug this whole toolset exists to catch: it paints once, then stops."""
    from trance import preview
    from trance.browser import Browser

    (tmp_path / "index.html").write_text("""
      <canvas id="c" width="60" height="60"></canvas>
      <script>
        const ctx = document.getElementById('c').getContext('2d');
        ctx.fillStyle = '#00f'; ctx.fillRect(0, 0, 40, 40);   /* and never again */
      </script>
    """, encoding="utf8")

    served = preview.serve(tmp_path, host="127.0.0.1", port=0)
    try:
        with Browser() as browser:
            browser.navigate(f"http://127.0.0.1:{served.port}/index.html", settle_frames=10)
            live = browser.liveness(20)
            # Not blank — it did paint — but nothing changes, which no amount of
            # looking at a single screenshot would tell you.
            assert live["after"]["uniform"] is False
            assert live["moving"] is False
    finally:
        served.stop()


@needs_chrome
def test_the_browsers_own_favicon_request_is_not_reported_as_a_defect(tmp_path):
    """Regression: a static server 404s /favicon.ico and Chrome logs it as a
    console error, so every visual check of every project reported a defect
    that was not in the project."""
    from trance import preview
    from trance.browser import Browser

    (tmp_path / "index.html").write_text("<canvas width=10 height=10></canvas>")
    served = preview.serve(tmp_path, host="127.0.0.1", port=0)
    try:
        with Browser() as browser:
            loaded = browser.navigate(f"http://127.0.0.1:{served.port}/index.html",
                                      settle_frames=5)
            assert loaded["errors"]["total"] == 0
    finally:
        served.stop()


@needs_chrome
def test_a_real_missing_file_is_still_reported_and_names_itself(tmp_path):
    """The other half: filtering the browser's noise must not filter the app's."""
    from trance import preview
    from trance.browser import Browser

    (tmp_path / "index.html").write_text(
        "<canvas width=10 height=10></canvas><script src='game.js'></script>")
    served = preview.serve(tmp_path, host="127.0.0.1", port=0)
    try:
        with Browser() as browser:
            loaded = browser.navigate(f"http://127.0.0.1:{served.port}/index.html",
                                      settle_frames=5)
            assert loaded["errors"]["total"] >= 1
            # "Failed to load resource: 404" names nothing anyone could fix.
            assert any("game.js" in line for line in loaded["errors"]["console"])
    finally:
        served.stop()


def test_screenshots_are_gitignored_even_in_an_older_project(tmp_path):
    """Regression: the ignore list was all-or-nothing, so a project set up by an
    earlier trance never picked up anything added afterwards — and the first
    thing added afterwards was PNGs."""
    from trance import vcs

    older = tmp_path / ".gitignore"
    older.write_text("node_modules\n"
                     "# trance's index — regenerated, not source\n"
                     ".trance/graph.db\n", encoding="utf8")

    assert vcs.ignore_trance_files(tmp_path) is True
    now = older.read_text(encoding="utf8")
    assert ".trance/shots/" in now
    assert now.count(".trance/graph.db\n") == 1        # not duplicated
    assert now.startswith("node_modules")              # nothing of theirs lost
    assert vcs.ignore_trance_files(tmp_path) is False  # nothing left to add


def test_a_new_default_loop_reaches_a_setup_that_already_exists(tmp_path):
    """Regression: the loop file existing meant "seeded already", so a loop
    added in a later version never appeared for anyone who had run trance
    before it shipped."""
    import json

    from trance.agents.store import LoopStore

    path = tmp_path / "loops.json"
    # A store written before visual-test-and-fix existed, with an edit of their
    # own in it.
    first = LoopStore(path)
    mine = first.get("visual-test-and-fix")
    mine.name = "my-own-loop"
    mine.description = "my own wording"
    first.upsert(mine)
    data = json.loads(path.read_text(encoding="utf8"))
    data["loops"] = [l for l in data["loops"] if l["name"] != "visual-test-and-fix"]
    path.write_text(json.dumps(data), encoding="utf8")

    after = LoopStore(path)
    assert after.get("visual-test-and-fix") is not None      # the upgrade lands
    assert after.get("my-own-loop").description == "my own wording"   # theirs survives


@needs_chrome
def test_a_keypress_reports_whether_it_arrived_and_whether_it_did_anything(tmp_path):
    """Regression: press_key only ever said "Pressed Space", so an agent had no
    evidence a working keypress had worked — and reported that nothing
    happened when the game had in fact started."""
    from trance import preview
    from trance.browser import Browser

    (tmp_path / "index.html").write_text("""
      <canvas id="c" width="80" height="80"></canvas>
      <script>
        const ctx = document.getElementById('c').getContext('2d');
        let on = false;
        addEventListener('keydown', e => { if (e.code === 'Space') on = true; });
        (function draw() {
          ctx.fillStyle = on ? '#c00' : '#004';
          ctx.fillRect(0, 0, 80, 80);
          requestAnimationFrame(draw);
        })();
      </script>
    """, encoding="utf8")

    served = preview.serve(tmp_path, host="127.0.0.1", port=0)
    try:
        with Browser() as browser:
            browser.navigate(f"http://127.0.0.1:{served.port}/index.html", settle_frames=10)

            acted = browser.press("Space")
            assert acted["delivered"] == ["Space"]     # the page really got it
            assert acted["changed"] is True            # and did something with it

            # A key the app receives and ignores is a different finding from one
            # that never arrived, and the two have different fixes.
            ignored = browser.press("ArrowUp")
            assert ignored["delivered"] == ["ArrowUp"]
            assert ignored["changed"] is False
    finally:
        served.stop()


@needs_chrome
def test_an_app_that_swallows_the_event_cannot_hide_that_it_arrived(tmp_path):
    """The delivery check listens in the capture phase, so stopPropagation in
    the app's own handler does not make a delivered key look lost."""
    from trance import preview
    from trance.browser import Browser

    (tmp_path / "index.html").write_text("""
      <canvas id="c" width="40" height="40"></canvas>
      <script>
        addEventListener('keydown', e => { e.stopPropagation(); e.preventDefault(); }, true);
        const ctx = document.getElementById('c').getContext('2d');
        ctx.fillStyle = '#0a0'; ctx.fillRect(0, 0, 20, 20);
      </script>
    """, encoding="utf8")

    served = preview.serve(tmp_path, host="127.0.0.1", port=0)
    try:
        with Browser() as browser:
            browser.navigate(f"http://127.0.0.1:{served.port}/index.html", settle_frames=5)
            result = browser.press("Space")
            assert result["delivered"] == ["Space"]
            assert result["changed"] is False
    finally:
        served.stop()


def test_the_tool_says_which_of_the_two_things_went_wrong(tmp_path, monkeypatch):
    """The wording matters: "the app ignored you" and "the browser lost it" send
    an agent to different places."""
    tools = AgentTools(tmp_path, _role(), step_id="st-k")

    class _Session:
        def __init__(self, **kw):
            self.kw = kw

        def press(self, key, times=1, hold_frames=None):
            return {"key": key, "times": times, "frames": 20, "probe": {},
                    "held_frames": hold_frames or 8, **self.kw}

    for kw, want in [
        ({"delivered": [], "changed": None}, "did not receive the key at all"),
        ({"delivered": ["Space"], "changed": True}, "the screen changed"),
        ({"delivered": ["Space"], "changed": False}, "did not change"),
        ({"delivered": ["Space"], "changed": None}, "could not be compared"),
    ]:
        monkeypatch.setattr(type(tools), "visual",
                            property(lambda self, kw=kw: _Session(**kw)))
        out = tools.call("press_key", {"key": "Space"})
        assert out.ok is True and want in out.text, (kw, out.text)
        assert out.detail["delivered"] is bool(kw["delivered"])


def test_the_press_settle_is_long_enough_to_have_been_worth_measuring():
    """Regression: 20 frames after the start key showed one ghost of four on a
    real game — the rest were still leaving the pen two seconds later — so a
    look straight after a press failed a game that was working."""
    from trance import browser

    assert browser.PRESS_SETTLE_FRAMES >= 60


@needs_chrome
def test_waiting_lets_late_arrivals_appear_and_notices_a_loop_that_stops(tmp_path):
    from trance import preview
    from trance.browser import Browser

    # Draws one square immediately and a second only much later — the shape of
    # every "characters enter over the first seconds" screen. The threshold is
    # far out because navigate() itself drains for a second before returning,
    # which is already ~60 frames of the page's own loop.
    (tmp_path / "index.html").write_text("""
      <canvas id="c" width="80" height="80"></canvas>
      <script>
        const ctx = document.getElementById('c').getContext('2d');
        window.N = 0;
        (function draw() {
          ctx.fillStyle = '#000'; ctx.fillRect(0, 0, 80, 80);
          ctx.fillStyle = '#0f0'; ctx.fillRect(0, 0, 20, 20);
          if (++window.N > 400) { ctx.fillStyle = '#f00'; ctx.fillRect(40, 40, 20, 20); }
          if (window.N < 900) requestAnimationFrame(draw);      /* then it stops */
        })();
      </script>
    """, encoding="utf8")

    served = preview.serve(tmp_path, host="127.0.0.1", port=0)
    try:
        with Browser() as browser:
            browser.navigate(f"http://127.0.0.1:{served.port}/index.html", settle_frames=20)
            early = browser.probe()
            assert browser._eval("window.N") < 400        # nothing has arrived yet
            browser.wait_frames(400)
            late = browser.probe()
            # The second square arrived: judging the early frame would have
            # called a working page a broken one.
            assert late["hash"] != early["hash"]

            # And once the page's own draw loop stops, the frame counter keeps
            # counting — it runs on its own requestAnimationFrame chain, which
            # the browser goes on serving. So a dead draw loop is NOT "fewer
            # frames than asked"; it is the picture no longer changing, and
            # conflating the two is how a blocked tab and a dead render loop
            # would get reported as each other.
            browser.wait_frames(500)                 # past the page's cutoff
            assert browser.wait_frames(120) == 120   # frames still arrive
            assert browser.liveness(30)["moving"] is False   # but nothing moves
    finally:
        served.stop()


def test_wait_is_granted_with_the_browser_and_reports_a_blocked_page(tmp_path, monkeypatch):
    tools = AgentTools(tmp_path, _role())
    assert "wait" in {s["function"]["name"] for s in tools.specs()}

    class _Session:
        def wait(self, frames):
            return {"asked_frames": frames, "frames": 12, "changed": False,
                    "stalled": True, "probe": {}, "errors": {}}

    monkeypatch.setattr(type(tools), "visual", property(lambda self: _Session()))
    out = tools.call("wait", {"frames": 200})
    assert out.ok is True
    # Specifically *not* "the render loop is dead": frames keep arriving after an
    # app's draw loop stops, so falling short means the browser stopped serving
    # them — a blocked main thread. The two have different causes and fixes.
    assert "main thread is blocked" in out.text
    assert "render loop" not in out.text
    assert out.detail["stalled"] is True and out.detail["frames"] == 12

    # A silly number is clamped rather than hanging the step.
    class _Big(_Session):
        def wait(self, frames):
            assert frames <= 1200
            return super().wait(frames)

    monkeypatch.setattr(type(tools), "visual", property(lambda self: _Big()))
    assert tools.call("wait", {"frames": 10 ** 9}).ok is True


@needs_chrome
def test_a_change_to_one_colour_channel_is_not_invisible_to_the_hash(tmp_path):
    """Regression, and a bad one: the digest packed RGBA into a single int, so a
    change to red alone moved the hash by a multiple of 2^24 and vanished mod
    2^32. A red square appearing on a black canvas was reproducibly invisible —
    which reads as "the picture never changed", and to the liveness check as a
    dead render loop on an app that is working perfectly."""
    from trance import preview
    from trance.browser import Browser

    (tmp_path / "index.html").write_text("""
      <canvas id="c" width="80" height="80"></canvas>
      <script>
        const ctx = document.getElementById('c').getContext('2d');
        window.RED = false;
        addEventListener('keydown', () => { window.RED = true; });
        (function draw() {
          ctx.fillStyle = '#000'; ctx.fillRect(0, 0, 80, 80);
          // Red only. Nothing else about the canvas differs.
          if (window.RED) { ctx.fillStyle = '#f00'; ctx.fillRect(40, 40, 20, 20); }
          requestAnimationFrame(draw);
        })();
      </script>
    """, encoding="utf8")

    served = preview.serve(tmp_path, host="127.0.0.1", port=0)
    try:
        with Browser() as browser:
            browser.navigate(f"http://127.0.0.1:{served.port}/index.html", settle_frames=20)
            result = browser.press("Space")
            assert result["delivered"] == ["Space"]
            assert result["changed"] is True, "a red-only change was invisible to the hash"
    finally:
        served.stop()


def test_function_keys_and_editing_keys_are_known():
    """A game binds F1 for help and Escape for pause; an agent asking for one
    should not be told the key does not exist."""
    assert key_spec("F1")[2] == 112
    assert key_spec("F12")[2] == 123
    assert key_spec("Backspace")[2] == 8
    assert key_spec("PageDown")[2] == 34


@needs_chrome
def test_a_press_keeps_a_picture_of_each_side_of_itself(tmp_path):
    """"Nothing changed" is a claim about pixels. Without the pixels it cannot
    be checked, and a wrong one looks exactly like a right one."""
    from trance.agents.visual import VisualSession

    (tmp_path / "index.html").write_text("""
      <canvas id="c" width="60" height="60"></canvas>
      <script>
        const ctx = document.getElementById('c').getContext('2d');
        window.ON = false;
        addEventListener('keydown', e => { if (e.code === 'Space') window.ON = true; });
        (function draw() {
          ctx.fillStyle = window.ON ? '#0a0' : '#008';
          ctx.fillRect(0, 0, 60, 60);
          requestAnimationFrame(draw);
        })();
      </script>
    """, encoding="utf8")

    session = VisualSession(tmp_path, session_id="s", step_id="st-pair")
    try:
        session.open("index.html", settle_frames=10)

        pressed = session.press("Space")
        assert pressed["changed"] is True
        for side in ("shot_before", "shot_after"):
            saved = tmp_path / ".trance" / "shots" / pressed[side]
            assert saved.is_file() and saved.read_bytes().startswith(b"\x89PNG")
        assert pressed["shot_before"] != pressed["shot_after"]

        # A wait keeps both ends too — that is where "some time later" happens.
        waited = session.wait(30)
        assert waited["shot_before"] and waited["shot_after"]
    finally:
        session.close()


def test_the_key_and_wait_details_carry_their_screenshots(tmp_path, monkeypatch):
    tools = AgentTools(tmp_path, _role(), step_id="st-d")

    class _Session:
        def press(self, key, times=1, hold_frames=None):
            return {"key": key, "times": times, "frames": 60, "probe": {},
                    "delivered": ["Space"], "changed": False, "held_frames": hold_frames or 8,
                    "shot_before": "st-d/001.png", "shot_after": "st-d/002.png"}

        def wait(self, frames):
            return {"asked_frames": frames, "frames": frames, "changed": True,
                    "stalled": False, "probe": {}, "errors": {},
                    "shot_before": "st-d/003.png", "shot_after": "st-d/004.png"}

    monkeypatch.setattr(type(tools), "visual", property(lambda self: _Session()))
    key = tools.call("press_key", {"key": "Space"}).detail
    assert (key["shot_before"], key["shot_after"]) == ("st-d/001.png", "st-d/002.png")
    waited = tools.call("wait", {"frames": 60}).detail
    assert (waited["shot_before"], waited["shot_after"]) == ("st-d/003.png", "st-d/004.png")


# ------------------------------------------------------- comparing pictures

def _png(width, height, pixel_at):
    """A minimal RGBA PNG, so the diff can be tested without an image library."""
    import struct
    import zlib

    rows = bytearray()
    for y in range(height):
        rows.append(0)                                   # filter: none
        for x in range(width):
            rows += bytes(pixel_at(x, y))

    def chunk(kind, body):
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows)))
            + chunk(b"IEND", b""))


def test_two_screenshots_are_compared_as_pixels_and_counted():
    from trance.imagediff import compare

    black = _png(20, 10, lambda x, y: (0, 0, 0, 255))
    same = compare(black, black)
    assert same.identical and same.how == "bytes"        # no need to decode

    # One pixel differs, and only in red — the channel the old canvas hash was
    # blind to. It must be found, and counted as exactly one.
    nearly = _png(20, 10, lambda x, y: (255, 0, 0, 255) if (x, y) == (3, 4)
                  else (0, 0, 0, 255))
    one = compare(black, nearly)
    assert one.identical is False
    assert (one.differing, one.total, one.how) == (1, 200, "pixels")
    assert "1 of 200 pixels" in one.describe()

    half = _png(20, 10, lambda x, y: (9, 9, 9, 255) if x < 10 else (0, 0, 0, 255))
    assert compare(black, half).differing == 100
    assert abs(compare(black, half).fraction - 0.5) < 1e-9


def test_a_resized_canvas_counts_as_a_change_rather_than_an_error():
    from trance.imagediff import compare

    assert compare(_png(4, 4, lambda x, y: (0, 0, 0, 255)),
                   _png(8, 4, lambda x, y: (0, 0, 0, 255))).identical is False


def test_an_undecodable_image_still_gets_an_answer():
    """Falling back to bytes keeps a wrong "identical" off the screen — the one
    outcome that matters, since it is what a stuck app looks like."""
    from trance.imagediff import compare

    assert compare(b"not a png", b"not a png").identical is True      # same bytes
    assert compare(b"not a png", b"different").identical is False
    assert compare(b"", b"anything").identical is False               # nothing captured
    assert compare(b"not a png", b"different").how == "bytes"


def test_every_png_scanline_filter_decodes():
    """Chrome picks filters per row; getting one wrong would corrupt the rows
    after it and inflate every diff."""
    from trance.imagediff import _decode

    # A gradient gives the encoder a reason to use the interesting filters.
    original = _png(16, 16, lambda x, y: ((x * 13) % 256, (y * 7) % 256,
                                          (x + y) % 256, 255))
    width, height, step, pixels = _decode(original)
    assert (width, height, step) == (16, 16, 4)
    assert pixels[0:4] == bytearray((0, 0, 0, 255))
    at = (5 * 16 + 3) * 4
    assert pixels[at:at + 4] == bytearray(((3 * 13) % 256, (5 * 7) % 256, 8, 255))


@needs_chrome
def test_a_webgl_canvas_that_animates_is_not_reported_as_frozen(tmp_path):
    """Regression, and the one that reached the user: a WebGL canvas reads back
    empty, so the digest of that empty buffer was a CONSTANT — every probe
    agreed with every other and a moving game reported "nothing changed"
    forever, next to two screenshots that visibly differed."""
    from trance.agents.visual import VisualSession

    (tmp_path / "index.html").write_text("""
      <canvas id="c" width="120" height="120"></canvas>
      <script>
        const gl = document.getElementById('c').getContext('webgl');
        let t = 0;
        (function draw() {
          t += 0.05;
          gl.clearColor(Math.abs(Math.sin(t)), 0.2, 0.6, 1);
          gl.clear(gl.COLOR_BUFFER_BIT);
          requestAnimationFrame(draw);
        })();
      </script>
    """, encoding="utf8")

    session = VisualSession(tmp_path, session_id="s", step_id="st-gl")
    try:
        opened = session.open("index.html", settle_frames=10)
        # The canvas itself genuinely cannot answer — and says so rather than
        # inventing a hash that never changes.
        assert opened["probe"]["uniform"] is None
        assert opened["probe"]["hash"] is None

        waited = session.wait(30)
        assert waited["changed"] is True, "an animating WebGL canvas read as frozen"
        assert waited["diff"]["how"] == "pixels"
        assert waited["diff"]["differing"] > 0
    finally:
        session.close()


@needs_chrome
def test_a_key_is_held_long_enough_for_a_polling_game_to_see_it(tmp_path):
    """Regression, measured on a real Phaser game: ten presses of ArrowLeft
    dispatched as keyDown-then-keyUp moved the ship 0 pixels, while one press
    held for 40 frames moved it 249. Most games poll `isDown` once per frame, so
    a key that goes down and up between two frames is invisible to them — and
    the press reports itself delivered, correctly, while nothing moves."""
    from trance import preview
    from trance.browser import Browser

    # Counts only the frames on which the key was observed held, the way a game
    # loop sees it — not the events, which arrive either way.
    (tmp_path / "index.html").write_text("""
      <canvas id="c" width="80" height="80"></canvas>
      <script>
        const ctx = document.getElementById('c').getContext('2d');
        const down = {};
        addEventListener('keydown', e => { down[e.code] = true; });
        addEventListener('keyup', e => { down[e.code] = false; });
        window.HELD = 0;
        (function tick() {
          if (down.ArrowLeft) window.HELD += 1;
          ctx.fillStyle = down.ArrowLeft ? '#0f0' : '#008';
          ctx.fillRect(0, 0, 80, 80);
          requestAnimationFrame(tick);
        })();
      </script>
    """, encoding="utf8")

    served = preview.serve(tmp_path, host="127.0.0.1", port=0)
    try:
        with Browser() as browser:
            browser.navigate(f"http://127.0.0.1:{served.port}/index.html", settle_frames=20)

            browser.press("ArrowLeft")
            seen = browser._eval("window.HELD")
            assert seen >= 3, f"the game loop only saw the key on {seen} frames"

            # And holding longer is what moves something a long way, rather than
            # pressing many times.
            browser._eval("window.HELD = 0")
            browser.press("ArrowLeft", hold_frames=40)
            assert browser._eval("window.HELD") > seen
    finally:
        served.stop()


def test_the_hold_is_capped_and_reported(tmp_path, monkeypatch):
    tools = AgentTools(tmp_path, _role(), step_id="st-h")
    asked = {}

    class _Session:
        def press(self, key, times=1, hold_frames=None):
            asked["hold"] = hold_frames
            return {"key": key, "times": times, "frames": 60, "probe": {},
                    "delivered": ["ArrowLeft"], "changed": True, "held_frames": hold_frames}

    monkeypatch.setattr(type(tools), "visual", property(lambda self: _Session()))

    out = tools.call("press_key", {"key": "ArrowLeft", "hold": 40})
    assert asked["hold"] == 40
    assert "held for 40 frames" in out.text
    assert out.detail["held_frames"] == 40

    # A silly number must not hang the step.
    tools.call("press_key", {"key": "ArrowLeft", "hold": 10 ** 6})
    assert asked["hold"] == 600

    # Left alone, the browser's own default applies rather than zero.
    tools.call("press_key", {"key": "ArrowLeft"})
    assert asked["hold"] is None


def test_nothing_inspects_a_browser_that_was_never_sent_anywhere(tmp_path):
    """Regression: every tool starts the browser lazily, so calling one before
    open_page left it on about:blank — and about:blank is a white page that
    never changes. `wait` reported "the screen did not change at all", ok=True,
    beside two blank screenshots it called byte-for-byte identical. All true,
    all useless, and it cost a whole block of a loop."""
    # With a model configured, so `look` reaches the page check rather than
    # stopping earlier on having nothing to look with.
    tools = AgentTools(tmp_path, _role(), session_id="s", step_id="st",
                       vision_config=_vision_config())
    try:
        for name, args in (("wait", {"frames": 30}),
                           ("check_canvas", {}),
                           ("press_key", {"key": "Space"}),
                           ("look", {"question": "anything?"})):
            out = tools.call(name, args)
            assert out.ok is False, f"{name} answered about a blank page"
            assert "open_page first" in out.text, name
    finally:
        tools.close()


@needs_chrome
def test_the_tools_work_once_a_page_is_open(tmp_path):
    """And the guard must not be in the way of the normal case."""
    from trance.agents.visual import VisualSession

    (tmp_path / "index.html").write_text(
        "<canvas id=c width=40 height=40></canvas><script>"
        "const x=document.getElementById('c').getContext('2d');"
        "(function d(){x.fillStyle='#'+((Date.now()/16|0)%2?'0a0':'00a');"
        "x.fillRect(0,0,40,40);requestAnimationFrame(d)})();</script>",
        encoding="utf8")

    session = VisualSession(tmp_path, session_id="s", step_id="st")
    try:
        session.open("index.html", settle_frames=10)
        assert session.wait(20)["frames"] > 0
        assert session.check(10)["canvas"] is True
        assert session.press("Space")["delivered"] == ["Space"]
    finally:
        session.close()


def test_a_second_turn_does_not_write_over_the_first_ones_pictures(tmp_path):
    """The evidence a line points at has to still be there.

    The counter restarted for every agent turn while the folder stayed per
    step, so block two of a step wrote over block one's screenshots — and a
    line reading "nothing changed · byte-for-byte identical" then sat above two
    visibly different images, because the pair it had compared was gone.
    Measured on one real step: five of the last six "identical" claims pointed
    at files that differ.
    """
    from trance.agents.visual import VisualSession

    first = VisualSession(tmp_path, session_id="s1", step_id="st1")
    before = first.save(b"first-turn-before")
    after = first.save(b"first-turn-after")

    # A new turn on the same step: a fresh session object, as the loop makes.
    second = VisualSession(tmp_path, session_id="s1", step_id="st1")
    later = second.save(b"second-turn")

    assert len({before, after, later}) == 3, (before, after, later)
    shots = tmp_path / ".trance" / "shots"
    assert (shots / before).read_bytes() == b"first-turn-before"
    assert (shots / after).read_bytes() == b"first-turn-after"
    assert (shots / later).read_bytes() == b"second-turn"


def test_numbering_stays_readable_across_turns(tmp_path):
    """Continuing the count, not salting the name: the order they were taken in
    is the only thing that makes a folder of screenshots readable."""
    from trance.agents.visual import VisualSession

    VisualSession(tmp_path, step_id="st1").save(b"a")
    VisualSession(tmp_path, step_id="st1").save(b"b")
    third = VisualSession(tmp_path, step_id="st1").save(b"c")
    assert third.endswith("003.png"), third


# ------------------------------------------------------------------- film

def test_watch_is_offered_beside_look(tmp_path):
    tools = AgentTools(tmp_path, _role())
    names = {s["function"]["name"] for s in tools.specs()}
    assert "watch" in names
    brief = permissions_brief(_role())
    assert "watch" in brief and "motion" in brief


def test_a_film_reaches_the_vision_model_whole_and_the_ui_as_paths(tmp_path, monkeypatch):
    """The point of the burst is that the model sees every frame — not the two
    endpoints — and that the console can play the same frames back."""
    from trance.agents import tools as tools_module

    filmed = {"pngs": [b"png1", b"png2", b"png3"],
              "shots": ["st/001.png", "st/002.png", "st/003.png"],
              "frames": 120, "asked_frames": 120, "frames_between": 60,
              "clipped": True, "motion": [0.2, 0.0], "moving": True}
    sent = {}

    class _Visual:
        def film(self, frames, shots):
            sent["asked"] = (frames, shots)
            return dict(filmed)

    def _sequence(pngs, question, config, *, checks, frames_between, cancel_token=""):
        sent["pngs"] = list(pngs)
        sent["question"] = question
        return {"answer": "The sprite moves smoothly right. All frames consistent.",
                "prompt": "p", "model": "m", "preset": "vlm", "usage": {"total_tokens": 9}}

    monkeypatch.setattr("trance.vision.look_sequence", _sequence)
    tools = AgentTools(tmp_path, _role(), vision_config=ModelConfig())
    tools._visual = _Visual()

    out = tools.call("watch", {"question": "does the player move right?",
                               "frames": 120, "shots": 3})

    assert out.ok is True
    assert sent["pngs"] == [b"png1", b"png2", b"png3"]      # every frame, in order
    assert out.detail["kind"] == "film"
    assert out.detail["shots"] == ["st/001.png", "st/002.png", "st/003.png"]
    assert out.detail["answer"].startswith("The sprite moves")
    # The measured motion is stated even though a model also answered.
    assert "changed in 1 of 2 intervals" in out.text


def test_a_film_without_a_vision_model_still_reports_what_it_measured(tmp_path):
    """No model is not no answer: the frames and the diffs are facts, and the
    commonest question — did anything move at all — needs no model."""
    class _Visual:
        def film(self, frames, shots):
            return {"pngs": [b"a", b"b"], "shots": ["st/001.png", "st/002.png"],
                    "frames": 60, "asked_frames": 60, "frames_between": 60,
                    "clipped": True, "motion": [0.0], "moving": False}

    tools = AgentTools(tmp_path, _role(), vision_config=None)
    tools._visual = _Visual()
    out = tools.call("watch", {"question": "does it flicker?"})

    assert out.ok is True
    assert out.detail["kind"] == "film"
    assert out.detail["moving"] is False
    assert "No vision model" in out.text
    assert "changed in 0 of 1 intervals" in out.text



# ----------------------------------------------------------- the dev server

def test_a_vite_project_gets_its_own_dev_server_behind_the_page(tmp_path, monkeypatch):
    """Served statically, a Vite app loads and dies on its first bare import —
    a visual test of one could only ever photograph the failure. And its dev
    server transpiles without typechecking, so a broken `npm run build` does
    not stop the app being judged while the type errors are someone's task."""
    from types import SimpleNamespace

    from trance import preview
    from trance.agents.visual import VisualSession

    project = tmp_path / "app"
    project.mkdir()
    (project / "index.html").write_text("<canvas></canvas>", encoding="utf8")
    (project / "vite.config.ts").write_text("export default {}", encoding="utf8")
    (project / "package.json").write_text(
        '{"scripts": {"dev": "vite"}}', encoding="utf8")

    started = {}

    def _run_dev(directory, command, *, log_dir=None, **_kw):
        started["dir"], started["command"] = str(directory), command
        return SimpleNamespace(port=5191, alive=lambda: True,
                               stop=lambda: started.__setitem__("stopped", True))

    monkeypatch.setattr(preview, "run_dev", _run_dev)
    session = VisualSession(project)

    url = session._serve("index.html")
    assert url == "http://127.0.0.1:5191/"
    assert started["command"] == "npm run dev"
    assert session.dev_command == "npm run dev"

    session.close()
    assert started.get("stopped") is True


def test_a_dev_server_that_will_not_start_says_its_last_words(tmp_path, monkeypatch):
    """The log tail is the diagnosis — a missing dependency, a broken config —
    and without it the agent only knows that nothing loaded."""
    import pytest as _pytest

    from trance import preview
    from trance.agents.visual import VisualSession
    from trance.browser import BrowserUnavailable

    project = tmp_path / "app"
    project.mkdir()
    (project / "index.html").write_text("x", encoding="utf8")
    (project / "vite.config.ts").write_text("export default {}", encoding="utf8")
    (project / "package.json").write_text('{"scripts": {"dev": "vite"}}', encoding="utf8")

    def _refuse(directory, command, *, log_dir=None, **_kw):
        raise preview.DevServerFailed("exited before it served anything",
                                      output="Error: Cannot find module 'vite'")

    monkeypatch.setattr(preview, "run_dev", _refuse)
    with _pytest.raises(BrowserUnavailable) as raised:
        VisualSession(project)._serve("index.html")
    assert "npm run dev" in str(raised.value)
    assert "Cannot find module 'vite'" in str(raised.value)


def test_a_plain_page_is_still_served_statically(tmp_path, monkeypatch):
    """No bundler, no dev server: a folder of HTML has nothing to build, and
    starting npm for it would be pure ceremony."""
    from trance import preview
    from trance.agents.visual import VisualSession

    project = tmp_path / "plain"
    project.mkdir()
    (project / "index.html").write_text("<h1>hi</h1>", encoding="utf8")

    monkeypatch.setattr(preview, "run_dev",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("started npm")))
    session = VisualSession(project)
    try:
        url = session._serve("index.html")
        assert url.startswith("http://127.0.0.1:")
        assert session.dev_command == ""
    finally:
        session.close()


# ------------------------------------------------------------------ clicking

def test_click_is_offered_and_refuses_an_empty_target(tmp_path):
    tools = AgentTools(tmp_path, _role())
    names = {s["function"]["name"] for s in tools.specs()}
    assert "click" in names

    out = tools.call("click", {})
    assert out.ok is False and "visible words" in out.text


def test_a_click_that_finds_nothing_says_what_is_clickable(tmp_path):
    """"Not found" strands the agent; the list of what *is* there is the next
    move handed over."""
    class _Visual:
        def click(self, text="", x=None, y=None):
            return {"found": False, "text": text,
                    "candidates": ["Create game", "Join game", "Settings"]}

    tools = AgentTools(tmp_path, _role())
    tools._visual = _Visual()
    out = tools.call("click", {"text": "Start"})
    assert out.ok is False
    assert "Create game" in out.text and "Join game" in out.text


def test_a_still_screen_is_not_reported_as_a_dead_render_loop(tmp_path):
    """Seen live: a multiplayer lobby, still because it was waiting for a
    click, reported as "FROZEN — the render loop is not running" — and the
    agent went chasing a bug that was a menu."""
    class _Visual:
        def check(self, frames):
            return {"canvas": True, "canvases": 1, "size": "1280x800",
                    "blank": False, "moving": False, "frames": frames,
                    "read_via": "shot", "note": "",
                    "errors": {"console": [], "requests": [], "page": []}}

    tools = AgentTools(tmp_path, _role())
    tools._visual = _Visual()
    out = tools.call("check_canvas", {"frames": 30})
    assert "FROZEN" not in out.text
    assert "waiting for input" in out.text
    assert "click or press" in out.text


def test_the_procedure_tells_the_tester_a_still_lobby_is_not_a_bug():
    from trance.agents.roles import BUILTIN_ROLES

    prompt = BUILTIN_ROLES["visual-tester"].system_prompt
    assert "click the button by the words on it" in prompt
    assert "unchanging lobby is not a frozen app" in prompt


@needs_chrome
def test_a_real_button_can_be_clicked_by_its_words(tmp_path):
    """End to end through a real Chrome: find the button by its text, click
    it, and watch the page change."""
    from trance.agents.visual import VisualSession

    project = tmp_path / "lobby"
    project.mkdir()
    (project / "index.html").write_text("""
<!doctype html><html><body>
<h1>ZERGLING RUSH</h1>
<button onclick="document.body.innerHTML='<h2 id=go>game on</h2>'">Join game</button>
</body></html>""", encoding="utf8")

    session = VisualSession(project)
    try:
        session.open("index.html", settle_frames=5)
        result = session.click(text="join game")          # case-insensitive
        assert result["found"] is True
        assert result["label"] == "Join game"
        assert result["delivered"] is True

        missing = session.click(text="Quit")
        assert missing["found"] is False
        assert missing["candidates"] == []                # the button is gone: game on
    finally:
        session.close()


# ------------------------------------------------------- the identity check

@needs_chrome
def test_open_page_notices_when_something_else_answers(tmp_path, monkeypatch):
    """Watched live: a Docker container squatted the expected port, answered
    200 with its own page, and two agents spent an hour debugging a chatbot's
    HTML as if it were their game. The page's own title against the project's
    is the mechanical version of "check the endpoint is our app"."""
    from trance import preview
    from trance.agents.visual import VisualSession

    # The project's page and the impostor that will actually answer.
    project = tmp_path / "game"
    project.mkdir()
    (project / "index.html").write_text(
        "<html><head><title>Zergling Rush</title></head><body>game</body></html>",
        encoding="utf8")
    impostor_root = tmp_path / "other"
    impostor_root.mkdir()
    (impostor_root / "index.html").write_text(
        "<html><head><title>Open WebUI</title></head><body>chat</body></html>",
        encoding="utf8")
    impostor = preview.serve(impostor_root, host="127.0.0.1", port=0)

    session = VisualSession(project)
    try:
        # Force the session at the impostor's server — the squatted-port shape.
        monkeypatch.setattr(session, "_serve",
                            lambda page: f"http://127.0.0.1:{impostor.port}/index.html")
        found = session.open("index.html", settle_frames=5)
        assert found["wrong_app"] is True
        assert found["title"] == "Open WebUI"
        assert found["expected_title"] == "Zergling Rush"

        # And served honestly, no alarm.
        monkeypatch.undo()
        honest = session.open("index.html", settle_frames=5)
        assert honest["wrong_app"] is False
    finally:
        session.close()
        impostor.stop()


def test_the_wrong_app_is_said_first_and_loudest(tmp_path):
    class _Visual:
        page = "index.html"
        dev_command = ""
        def open(self, path="", settle_frames=60):
            return {"page": "index.html", "url": "http://127.0.0.1:3000/",
                    "frames": 60, "asked_frames": 60,
                    "errors": {"console": [], "requests": [], "page": []},
                    "probe": {}, "dev_server": "", "dev_note": "",
                    "title": "Open WebUI", "expected_title": "Zergling Rush",
                    "wrong_app": True, "needs_build": False, "build_command": ""}

    tools = AgentTools(tmp_path, _role())
    tools._visual = _Visual()
    out = tools.call("open_page", {"path": "index.html"})
    assert out.text.startswith("STOP: what answered is NOT this project")
    assert "Open WebUI" in out.text and "Zergling Rush" in out.text
    assert "Report the conflict" in out.text


def test_the_tester_is_told_to_judge_only_the_handed_url():
    from trance.agents.roles import BUILTIN_ROLES

    prompt = BUILTIN_ROLES["visual-tester"].system_prompt
    assert "judge only the URL it hands you" in prompt
    assert "Do not start servers yourself" in prompt


def test_the_coders_are_told_ports_come_from_the_environment():
    from trance.agents.roles import BUILTIN_ROLES

    for name in ("developer", "developer"):
        prompt = BUILTIN_ROLES[name].system_prompt
        assert "process.env.PORT" in prompt, name
        assert "not yours to fight" in prompt, name


# ---------------------------------------------------------------- the playbook

def test_the_playbook_reaches_the_visual_tester_and_nobody_else(tmp_path, monkeypatch):
    """The tester has no file tools — judges must not rewrite the evidence —
    so the team's driving instructions arrive in the prompt or not at all.
    Coders read files themselves; the playbook in their prompt would be
    noise they already own."""
    from trance.agents.roles import AgentRole
    from trance.agents.runner import run_agent
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.providers.base import ChatResponse

    (tmp_path / "PLAYBOOK.md").write_text(
        "## How to reach gameplay\n"
        "Type any name, click 'Join game', click 'Ready'.\n", encoding="utf8")

    seen = {}

    class Model:
        def complete(self, messages, tools=None, **kwargs):
            seen["prompt"] = str(messages[1]["content"])
            return ChatResponse(text="fine\n\nOUTCOME: SUCCESS", finish_reason="stop")

    monkeypatch.setattr("trance.agents.runner.client_for", lambda config: Model())

    looker = AgentRole(name="vt", title="VT", description="", system_prompt="p",
                       paths=[], toolsets=["browser"])
    run_agent(role=looker, task="judge it", project=tmp_path, config=ModelConfig(),
              bus=EventBus(), session_id="s", step_id="st")
    assert "How to drive this app" in seen["prompt"]
    assert "Join game" in seen["prompt"]

    coder = AgentRole(name="dev", title="Dev", description="", system_prompt="p",
                      paths=["**"], toolsets=["files"])
    run_agent(role=coder, task="build it", project=tmp_path, config=ModelConfig(),
              bus=EventBus(), session_id="s", step_id="st")
    assert "How to drive this app" not in seen["prompt"]


def test_a_project_without_a_playbook_promises_nothing(tmp_path, monkeypatch):
    from trance.agents.roles import AgentRole
    from trance.agents.runner import run_agent
    from trance.config import ModelConfig
    from trance.events import EventBus
    from trance.providers.base import ChatResponse

    seen = {}

    class Model:
        def complete(self, messages, tools=None, **kwargs):
            seen["prompt"] = str(messages[1]["content"])
            return ChatResponse(text="ok\n\nOUTCOME: SUCCESS", finish_reason="stop")

    monkeypatch.setattr("trance.agents.runner.client_for", lambda config: Model())
    looker = AgentRole(name="vt", title="VT", description="", system_prompt="p",
                       paths=[], toolsets=["browser"])
    run_agent(role=looker, task="judge it", project=tmp_path, config=ModelConfig(),
              bus=EventBus(), session_id="s", step_id="st")
    assert "How to drive this app" not in seen["prompt"]


def test_the_team_is_told_to_keep_the_playbook_and_the_tester_to_follow_it():
    from trance.agents.roles import BUILTIN_ROLES

    for name in ("developer", "developer", "developer"):
        prompt = BUILTIN_ROLES[name].system_prompt
        assert "PLAYBOOK.md" in prompt, name
        assert "How to reach gameplay" in prompt, name

    tester = BUILTIN_ROLES["visual-tester"].system_prompt
    assert "follow its entry steps" in tester
    assert "is itself a finding" in tester


# ---------------- honest movement wording: numbers graded, located, judged

def test_a_tiny_pixel_diff_is_not_called_a_response(tmp_path):
    """Found live: a frozen game flickered 0.05-0.29% per keypress, the tool
    said "the screen changed — the app responded", and the tester built a
    PASS on that sentence over its own eyes."""
    from trance.agents.tools import AgentTools
    from trance.agents.roles import AgentRole

    role = AgentRole(name="vt", title="VT", description="", system_prompt="p",
                     paths=[], toolsets=["browser"])
    tools = AgentTools(tmp_path, role)

    class _Visual:
        def press(self, key, times=1, hold_frames=None):
            return {"key": key, "times": 1, "delivered": ["W"], "changed": True,
                    "frames": 60, "held_frames": hold_frames, "probe": {},
                    "diff": {"fraction": 0.0013, "described":
                             "The two screenshots differ in 647 of 506700 pixels (0.13%)."}}

    tools._visual = _Visual()
    out = tools.call("press_key", {"key": "W", "hold": 60})
    assert "flicker or a HUD tick" in out.text
    assert "it did not" in out.text
    assert "the app responded" not in out.text


def test_a_substantial_diff_still_reads_as_the_app_responding(tmp_path):
    from trance.agents.tools import AgentTools
    from trance.agents.roles import AgentRole

    role = AgentRole(name="vt", title="VT", description="", system_prompt="p",
                     paths=[], toolsets=["browser"])
    tools = AgentTools(tmp_path, role)

    class _Visual:
        def click(self, text="", x=None, y=None):
            return {"found": True, "x": 10, "y": 10, "delivered": True,
                    "changed": True, "frames": 60,
                    "diff": {"fraction": 0.98, "described": "big change"}}

    tools._visual = _Visual()
    out = tools.call("click", {"x": 10, "y": 10})
    assert "the app responded" in out.text


def test_the_diff_says_where_the_change_sits():
    """A HUD tick is a small patch; the scene moving is most of the frame —
    the location is the difference between them, so the sentence carries it."""
    from trance.imagediff import compare

    still = _png(60, 40, lambda x, y: (10, 10, 10, 255))
    hud_tick = _png(60, 40, lambda x, y: (200, 0, 0, 255)
                    if 50 <= x < 56 and 2 <= y < 6 else (10, 10, 10, 255))
    diff = compare(still, hud_tick)
    assert diff.box == (50, 2, 6, 4)
    assert "a patch of the screen, not the scene moving" in diff.describe()

    moved = _png(60, 40, lambda x, y: (0, int(x * 2) % 255, int(y * 3) % 255, 255))
    big = compare(still, moved)
    assert "span most of the frame" in big.describe()


def test_move_mouse_reports_lock_and_an_unmoved_camera(tmp_path):
    from trance.agents.tools import AgentTools
    from trance.agents.roles import AgentRole

    role = AgentRole(name="vt", title="VT", description="", system_prompt="p",
                     paths=[], toolsets=["browser"])
    tools = AgentTools(tmp_path, role)
    assert "move_mouse" in {s["function"]["name"] for s in tools.specs()}

    class _Visual:
        def move_mouse(self, dx, dy, steps=12):
            return {"dx": dx, "dy": dy, "steps": steps, "delivered": True,
                    "moves_seen": 12, "movement": (200.0, 0.0), "locked": False,
                    "changed": False, "frames": 12, "diff": None}

    tools._visual = _Visual()
    out = tools.call("move_mouse", {"dx": 200, "dy": 0})
    # Measured: pointer lock never engages in the harness browser, for any
    # app — so its absence must read as an environment fact, never as the
    # app's failure. A working game was failed over this line once.
    assert "never is in this headless browser" in out.text
    assert "do not call the input broken" in out.text
    assert "the camera did not move" in out.text
    assert out.detail["kind"] == "mouse" and out.detail["locked"] is False


def test_the_tester_is_told_how_to_judge_movement():
    from trance.agents.roles import BUILTIN_ROLES

    prompt = BUILTIN_ROLES["visual-tester"].system_prompt
    assert "move_mouse" in prompt
    assert "Never conclude movement from diff percentages alone" in prompt
    assert "the frames win" in prompt
    assert "Pointer lock never engages in this browser" in prompt


def test_only_trances_own_main_chrome_is_an_orphan_candidate():
    """Found live: a leaked headless Chrome spun a WebGL game on software
    rendering at twelve cores for a day. The reaper must catch exactly the
    main process of trance-launched browsers — its group takes the zygotes
    and the GPU process with it — and never anything else on the machine."""
    from trance.browser import _PROFILE_MARK, orphan_browser_pids

    lines = {
        11: f"chrome --headless {_PROFILE_MARK}45517 about:blank",
        12: f"chrome --type=gpu-process {_PROFILE_MARK}45517",
        13: f"chrome --type=renderer {_PROFILE_MARK}45517",
        14: "chrome --headless --user-data-dir=/home/petrovs/.config/chrome",
        15: "firefox",
        16: f"chrome --headless {_PROFILE_MARK}60909 about:blank",
    }
    # 11 lost its trance (reparented to init); 16's owner is alive — a live
    # server's browser, or a parallel test worker's — and stays untouched.
    parents = {11: 1, 12: 11, 13: 11, 14: 1, 15: 1, 16: 54321}
    assert orphan_browser_pids(lines, parents) == [11]
