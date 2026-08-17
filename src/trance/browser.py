"""Drives a real browser over the Chrome DevTools Protocol.

Deliberately not Playwright. CDP is JSON-RPC 2.0 over a websocket — the same
shape as the MCP server next door — and `websockets` is already here because
uvicorn needs it, so a visual test costs no new dependency and no 150MB browser
download. What it costs instead is that we own the connection: see `close`.

The surface is six operations, not a general automation framework: load a page,
collect its errors, probe the canvas, screenshot it, send a key, tear it down.
That is what judging a step needs. Anything that wants selectors and
auto-waiting wants Playwright, and should say so rather than growing this file.

Why so much of this is about *canvas*: for a game, the DOM is one element and
every real thing is pixels. `document.querySelector('canvas')` tells you nothing
about whether the game drew, so the checks here are all about the surface —
whether it painted at all, and whether it is still painting.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from glob import glob
from pathlib import Path

from websockets.sync.client import connect

#: Where to find a browser, best first. Real Chrome before the snap: snap
#: confinement puts the profile somewhere it may not be able to write, and the
#: failure looks like a hang rather than an error.
CHROME_BINARIES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
#: Browsers a Playwright install left behind. Fine to borrow — we speak the
#: protocol, not their Python API, so the revision does not have to match
#: anything.
CHROME_CACHE_GLOB = "~/.cache/ms-playwright/chromium*/chrome-linux/chrome"

#: A page that never resolves its promise must not hang the step.
EVAL_TIMEOUT_S = 30.0
LAUNCH_TIMEOUT_S = 20.0
#: Enough frames that a game has drawn something past its first paint, few
#: enough that the check is over in a second at 60fps.
SETTLE_FRAMES = 60
#: Longest edge of a screenshot sent to a model. Above this an image costs more
#: tokens without showing anything more; the browser does the scaling.
MAX_SHOT_EDGE = 900

#: Frames to let run after a keypress before reporting what came of it. One
#: second, not the third of a second it was: measured on a real game, 20 frames
#: after the start key showed one ghost of four — the rest were still leaving
#: the pen at frame 240. Judging that screen fails a game that is working.
#: A press is still not a substitute for waiting; see `wait`.
PRESS_SETTLE_FRAMES = 60

#: How long a key stays down, in animation frames. Not zero, which is what
#: dispatching keyDown and keyUp back to back amounts to: a game that polls
#: `isDown` once per frame never observes a key that went down and up between
#: two frames. Measured on a real Phaser game — ten tapped presses of ArrowLeft
#: moved the ship 0 pixels; one press held for 40 frames moved it 249.
HOLD_FRAMES = 8


class BrowserUnavailable(RuntimeError):
    """No browser on this machine. The visual toolset degrades; runs do not."""


#: The marker every trance-launched Chrome carries in its command line. The
#: reaper keys on it, so it must match the profile naming in start().
_PROFILE_MARK = "--user-data-dir=" + str(
    Path(os.environ.get("TMPDIR", "/tmp")) / "trance-chrome-")


def orphan_browser_pids(cmdlines: dict[int, str],
                        parents: dict[int, int]) -> list[int]:
    """The *main* processes of trance-launched Chromes whose owner is dead.

    Three tests, all required: trance's own profile marker, so nothing else
    on the machine is ever touched; the main process, not a --type= child —
    its group holds the zygotes and the GPU process; and a parent of pid 1,
    because a browser whose launching trance died is reparented to init,
    while one owned by a *live* trance (this server's own, another server's,
    a test worker's) keeps its living parent and must not be touched. Pure,
    so the answer is testable without spawning anything.
    """
    return [pid for pid, line in cmdlines.items()
            if _PROFILE_MARK in line and "--type=" not in line
            and parents.get(pid) == 1]


def reap_orphan_browsers() -> int:
    """Kill trance-launched Chromes that no live trance owns.

    Called at server startup, when this trance owns none — so every one that
    matches is a leak from a server that died hard. Found live: two of them,
    each spinning a WebGL game on software rendering at twelve cores, one of
    them for an entire day. A browser closed with its turn is the design;
    this is the backstop for the deaths no close() runs after.
    """
    cmdlines: dict[int, str] = {}
    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes().replace(b"\0", b" ")
            stat = (entry / "stat").read_text(encoding="utf8", errors="replace")
        except OSError:
            continue
        pid = int(entry.name)
        cmdlines[pid] = raw.decode("utf8", "replace")
        # Field 4 of /proc/pid/stat, counted after the parenthesised comm —
        # which may itself contain spaces, hence the rpartition.
        try:
            parents[pid] = int(stat.rpartition(")")[2].split()[1])
        except (ValueError, IndexError):
            continue
    reaped = 0
    for pid in orphan_browser_pids(cmdlines, parents):
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            reaped += 1
        except (OSError, ProcessLookupError):
            continue
    if reaped:
        for profile in Path(os.environ.get("TMPDIR", "/tmp")).glob("trance-chrome-*"):
            shutil.rmtree(profile, ignore_errors=True)
    return reaped


def find_chrome() -> str | None:
    """A Chrome-family binary, or None. Never raises: absence is a normal state."""
    for name in CHROME_BINARIES:
        found = shutil.which(name)
        if found:
            return found
    cached = sorted(glob(os.path.expanduser(CHROME_CACHE_GLOB)))
    return cached[-1] if cached else None


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


#: Waits `n` animation frames, but resolves anyway after `ms`. Counting frames
#: rather than sleeping matters because a real game paces itself by
#: requestAnimationFrame, so a second means different amounts of game on
#: different machines.
#:
#: What falling short does NOT mean: this chain is our own, and the browser
#: keeps serving it after the app's draw loop stops. Fewer frames than asked
#: means the *browser* stopped — a blocked main thread or a throttled tab. An
#: app whose own loop died still produces frames here, and is caught instead by
#: the picture never changing.
_FRAMES_JS = """
new Promise(done => {{
  let seen = 0;
  const tick = () => {{ if (++seen >= {n}) done(seen); else requestAnimationFrame(tick); }};
  requestAnimationFrame(tick);
  setTimeout(() => done(seen), {ms});
}})
"""

#: Reads the canvas back into numbers. `uniform` answers "did it paint at all" —
#: a single flat colour is what a crashed game looks like, and it is
#: indistinguishable from a correct black background to anything but this check.
#: `hash` answers "is it still painting" when compared across frames.
#:
#: 2D first, WebGL second, and neither is guaranteed: a WebGL context without
#: preserveDrawingBuffer reads back empty, and a canvas tainted by a cross-origin
#: image throws. Those report `uniform: null` — unknown, not "fine". Claiming a
#: pass from a read that never happened is the one outcome worth avoiding.
_PROBE_JS = """
(() => {
  const all = [...document.querySelectorAll('canvas')];
  if (!all.length) return {canvas: false, count: 0};
  const c = all.map(el => ({el, r: el.getBoundingClientRect()}))
               .sort((a, b) => b.r.width * b.r.height - a.r.width * a.r.height)[0];
  const el = c.el, r = c.r;
  const out = {canvas: true, count: all.length, w: el.width, h: el.height,
               rect: {x: r.x + scrollX, y: r.y + scrollY, width: r.width, height: r.height},
               uniform: null, hash: null, how: null, note: null};
  // FNV-1a, byte by byte. The obvious `hash * 31 + pixel` does not work here:
  // packing RGBA into one int puts red in the top 8 bits, so a change to red
  // alone shifts the hash by a multiple of 2^24 and vanishes mod 2^32. That is
  // not a rare collision — a red square appearing on a black canvas was
  // reproducibly invisible to it, which reads as "the picture never changed"
  // and, for the liveness check, as a dead render loop on a working app.
  const digest = (data) => {
    const first = (data[0] << 24) | (data[1] << 16) | (data[2] << 8) | data[3];
    let uniform = true, hash = 2166136261;
    for (let i = 0; i < data.length; i += 4) {
      const px = (data[i] << 24) | (data[i+1] << 16) | (data[i+2] << 8) | data[i+3];
      if (px !== first) uniform = false;
      for (let b = 0; b < 4; b++) {
        hash = Math.imul(hash ^ data[i + b], 16777619);
      }
    }
    return {uniform, hash: hash >>> 0};
  };
  try {
    const ctx = el.getContext('2d');
    if (ctx) {
      Object.assign(out, digest(ctx.getImageData(0, 0, el.width, el.height).data));
      out.how = '2d';
      return out;
    }
  } catch (e) { out.note = e.name + ': ' + e.message; }
  try {
    const gl = el.getContext('webgl2') || el.getContext('webgl');
    if (gl) {
      const buf = new Uint8Array(el.width * el.height * 4);
      gl.readPixels(0, 0, el.width, el.height, gl.RGBA, gl.UNSIGNED_BYTE, buf);
      Object.assign(out, digest(buf));
      out.how = 'webgl';
      // Without preserveDrawingBuffer the buffer is cleared after compositing,
      // so an all-transparent read means "cannot tell", not "blank". The hash
      // has to go with it: the digest of an all-zero buffer is a CONSTANT, so
      // leaving it made every probe agree with every other one and reported a
      // moving game as "nothing changed" forever.
      if (out.uniform) {
        out.uniform = null;
        out.hash = null;
        out.note = 'webgl readback empty — cannot tell from the canvas';
      }
      return out;
    }
  } catch (e) { out.note = e.name + ': ' + e.message; }
  if (!out.note) out.note = 'no readable drawing context';
  return out;
})()
"""

#: Records keys the page receives, so a press can prove it arrived rather than
#: only that it was sent. Capture phase and `once`-free: an app that calls
#: stopPropagation in its own handler must not be able to hide the evidence.
#: Installed once per page and reset on each press.
_CLICK_HOOK_JS = """
(() => {
  window.__tranceClicks = [];
  if (!window.__tranceClickHook) {
    window.__tranceClickHook = true;
    addEventListener('click',
      e => window.__tranceClicks.push(Math.round(e.clientX) + ',' + Math.round(e.clientY)),
      true);
  }
  return true;
})()
"""

#: Find something clickable by the words on it. Obvious clickables first, so
#: "Join" hits the Join button rather than a paragraph that mentions joining;
#: any element with exactly that text is the fallback for div-soup UIs.
_FIND_CLICK_JS = r"""
(() => {
  const want = %s.trim().toLowerCase();
  const label = el => (el.innerText || el.value || el.getAttribute('aria-label') || '')
    .trim().replace(/\s+/g, ' ');
  const obvious = [...document.querySelectorAll(
    'button, a, [role="button"], input[type="button"], input[type="submit"], ' +
    'summary, label, [onclick]')];
  let hit = obvious.find(el => label(el).toLowerCase() === want)
         || obvious.find(el => label(el).toLowerCase().includes(want));
  if (!hit) {
    const all = [...document.querySelectorAll('body *')]
      .filter(el => !el.children.length && label(el).toLowerCase() === want);
    hit = all[0];
  }
  if (!hit) {
    return {found: false,
            candidates: [...new Set(obvious.map(label).filter(Boolean))].slice(0, 12)};
  }
  hit.scrollIntoView({block: 'center', inline: 'center'});
  const r = hit.getBoundingClientRect();
  return {found: true, x: r.x + r.width / 2, y: r.y + r.height / 2,
          tag: hit.tagName.toLowerCase(), label: label(hit).slice(0, 60)};
})()
"""

_MOUSE_HOOK_JS = """
window.__tranceMouse = {moves: 0, dx: 0, dy: 0, locked: !!document.pointerLockElement};
if (!window.__tranceMouseHooked) {
  window.__tranceMouseHooked = true;
  document.addEventListener('mousemove', (e) => {
    const m = window.__tranceMouse; if (!m) return;
    m.moves += 1;
    m.dx += (e.movementX || 0);
    m.dy += (e.movementY || 0);
    m.locked = !!document.pointerLockElement;
  }, {capture: true, passive: true});
}
"""

_KEY_HOOK_JS = """
(() => {
  window.__tranceKeys = [];
  if (!window.__tranceKeyHook) {
    window.__tranceKeyHook = true;
    addEventListener('keydown', e => window.__tranceKeys.push(e.code || e.key), true);
  }
  return true;
})()
"""

#: keyCode still matters: plenty of game input code reads `event.keyCode`, and a
#: key event without one silently does nothing.
KEYS = {
    "Space":      (" ", "Space", 32, " "),
    "Enter":      ("Enter", "Enter", 13, "\r"),
    "Escape":     ("Escape", "Escape", 27, None),
    "ArrowUp":    ("ArrowUp", "ArrowUp", 38, None),
    "ArrowDown":  ("ArrowDown", "ArrowDown", 40, None),
    "ArrowLeft":  ("ArrowLeft", "ArrowLeft", 37, None),
    "ArrowRight": ("ArrowRight", "ArrowRight", 39, None),
    "Tab":        ("Tab", "Tab", 9, None),
    "Backspace":  ("Backspace", "Backspace", 8, None),
    "Delete":     ("Delete", "Delete", 46, None),
    "Home":       ("Home", "Home", 36, None),
    "End":        ("End", "End", 35, None),
    "PageUp":     ("PageUp", "PageUp", 33, None),
    "PageDown":   ("PageDown", "PageDown", 34, None),
    # Games bind these for pause, help and debug overlays, and an agent that
    # asks for one should not be told the key does not exist.
    **{f"F{n}": (f"F{n}", f"F{n}", 111 + n, None) for n in range(1, 13)},
}


def key_spec(name: str) -> tuple[str, str, int, str | None]:
    """(key, code, keyCode, text) for a named key or a single character."""
    if name in KEYS:
        return KEYS[name]
    canon = {k.lower(): k for k in KEYS}
    if name.lower() in canon:
        return KEYS[canon[name.lower()]]
    if len(name) == 1:
        upper = name.upper()
        code = f"Key{upper}" if name.isalpha() else f"Digit{name}"
        return (name, code, ord(upper), name)
    raise ValueError(f"unknown key {name!r}; try one of {', '.join(sorted(KEYS))} "
                     f"or a single character")


@dataclass
class PageErrors:
    """What the page complained about, split by how much it means."""

    console: list[str] = field(default_factory=list)
    exceptions: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.console) + len(self.exceptions) + len(self.failed_requests)

    def to_dict(self) -> dict:
        return {"console": self.console, "exceptions": self.exceptions,
                "failed_requests": self.failed_requests, "total": self.total}


#: Requests the *browser* makes on its own behalf, which no page asked for. A
#: static server answers /favicon.ico with a 404 and Chrome logs it as a console
#: error — so without this every visual check of every project reports a defect
#: that is not in the project, and a tester agent fails a step over it.
BROWSER_NOISE = ("/favicon.ico", "/apple-touch-icon", "/.well-known/")


def _is_browser_noise(url: str) -> bool:
    return any(part in (url or "") for part in BROWSER_NOISE)


def _differs(before: dict, after: dict) -> bool | None:
    """Whether two probes show a different picture. None = could not tell."""
    if before.get("hash") is None or after.get("hash") is None:
        return None
    return before["hash"] != after["hash"]


class _Killable:
    """Lets Stop reach a browser that is mid-page-load, like an in-flight call."""

    def __init__(self, process: subprocess.Popen):
        self.process = process
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True
        _kill(self.process)


def _kill(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass


class Browser:
    """One headless Chrome and one page in it.

    Started lazily and kept for the length of a step, because a game that needs
    a keypress to start has to still be running when the screenshot is taken —
    a fresh browser per tool call would only ever photograph the title screen.
    """

    def __init__(self, binary: str | None = None, width: int = 1280, height: int = 800,
                 cancel_token: str = ""):
        self.binary = binary or find_chrome()
        if not self.binary:
            raise BrowserUnavailable(
                "no Chrome or Chromium found. Install one, or leave the browser "
                "toolset off this agent — every other toolset works without it.")
        self.width, self.height = width, height
        self.cancel_token = cancel_token
        self.process: subprocess.Popen | None = None
        self._ws = None
        self._n = 0
        self._session = ""
        self._handle: _Killable | None = None
        #: requestId -> URL, so a failure can name what failed.
        self._requests: dict[str, str] = {}
        self.errors = PageErrors()
        self.url = ""

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._ws is not None:
            return
        port = _free_port()
        profile = Path(os.environ.get("TMPDIR", "/tmp")) / f"trance-chrome-{port}"
        self.process = subprocess.Popen(
            [self.binary, "--headless", "--disable-gpu", "--no-sandbox",
             # Software WebGL. Without it a WebGL game renders nothing headless
             # and the screenshot is a black rectangle that looks like a bug.
             "--enable-unsafe-swiftshader", "--hide-scrollbars", "--mute-audio",
             "--disable-dev-shm-usage", f"--window-size={self.width},{self.height}",
             f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self._handle = _Killable(self.process)
        from .providers.base import register_inflight
        register_inflight(self.cancel_token, self._handle)

        endpoint = self._wait_for_devtools(port)
        # No keepalive pings. The library's default (ping every 20s, fail on a
        # 20s silence) kills the connection with "1011 keepalive ping timeout"
        # whenever software-rendered WebGL freezes Chrome's loop past 20s —
        # which a heavy three.js scene on swiftshader does routinely. This is
        # a local socket to a child process we own: if Chrome dies the socket
        # closes by itself, so a ping proves nothing a read doesn't.
        self._ws = connect(endpoint, max_size=None, open_timeout=LAUNCH_TIMEOUT_S,
                           ping_interval=None)
        target = self._call("Target.createTarget", {"url": "about:blank"})
        self._session = self._call(
            "Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})["sessionId"]
        for domain in ("Page", "Runtime", "Log", "Network"):
            self._call(f"{domain}.enable", session=True)
        # Fixed metrics so two runs of the same page produce the same image.
        # Without this a screenshot depends on the host's display scaling, and
        # comparing one against another means comparing two different pictures.
        self._call("Emulation.setDeviceMetricsOverride",
                   {"width": self.width, "height": self.height,
                    "deviceScaleFactor": 1, "mobile": False}, session=True)

    def _wait_for_devtools(self, port: int) -> str:
        import urllib.error
        import urllib.request

        deadline = time.monotonic() + LAUNCH_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise BrowserUnavailable(
                    f"{self.binary} exited immediately (code {self.process.returncode}).")
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json/version", timeout=1) as response:
                    return json.load(response)["webSocketDebuggerUrl"]
            except (urllib.error.URLError, OSError, ValueError, KeyError):
                time.sleep(0.2)
        _kill(self.process)
        raise BrowserUnavailable(f"{self.binary} did not open a debugging port in "
                                 f"{LAUNCH_TIMEOUT_S:.0f}s.")

    def close(self) -> None:
        """Always call this. We launched the process, so we own its death."""
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:                       # noqa: BLE001 — already gone
                pass
            self._ws = None
        if self._handle is not None:
            from .providers.base import clear_inflight
            clear_inflight(self.cancel_token, self._handle)
            self._handle = None
        _kill(self.process)
        self.process = None

    def __enter__(self) -> "Browser":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ------------------------------------------------------------- protocol

    def _call(self, method: str, params: dict | None = None, session: bool = False,
              timeout: float = EVAL_TIMEOUT_S) -> dict:
        """One request, and the matching reply.

        Events arrive interleaved with replies on the same socket, so anything
        that is not our reply is filed and read back later by `drain`.
        """
        if self._ws is None:
            raise BrowserUnavailable("the browser is not running")
        self._n += 1
        message = {"id": self._n, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = self._session
        self._ws.send(json.dumps(message))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserUnavailable(f"{method} did not answer within {timeout:.0f}s")
            frame = json.loads(self._ws.recv(timeout=remaining))
            if frame.get("id") == self._n:
                if "error" in frame:
                    raise BrowserUnavailable(f"{method}: {frame['error'].get('message')}")
                return frame.get("result", {})
            if "method" in frame:
                self._file(frame)

    def _file(self, frame: dict) -> None:
        """Record a page complaint. Warnings are not errors and are dropped."""
        params = frame.get("params", {})
        if frame["method"] == "Log.entryAdded":
            entry = params.get("entry", {})
            if entry.get("level") != "error":
                return
            url = entry.get("url", "")
            if _is_browser_noise(url):
                return
            # The console text alone is "Failed to load resource: 404", which
            # names nothing an agent could go and fix. The URL is the finding.
            text = entry.get("text", "")
            self.errors.console.append((f"{text} — {url}" if url else text)[:400])
        elif frame["method"] == "Runtime.exceptionThrown":
            detail = params.get("exceptionDetails", {})
            text = detail.get("exception", {}).get("description") or detail.get("text", "")
            self.errors.exceptions.append(str(text)[:400])
        elif frame["method"] == "Network.requestWillBeSent":
            # Kept so loadingFailed, which carries only a requestId, can say
            # which URL it was.
            request = params.get("request", {})
            if request.get("url"):
                self._requests[params.get("requestId", "")] = request["url"][:300]
        elif frame["method"] == "Network.loadingFailed":
            url = self._requests.get(params.get("requestId", ""), "")
            if not params.get("canceled") and not _is_browser_noise(url):
                self.errors.failed_requests.append(
                    f"{params.get('type', '?')}: {params.get('errorText', 'failed')}"
                    + (f" — {url}" if url else ""))

    def drain(self, seconds: float = 0.0) -> None:
        """Read whatever the page has said since the last call."""
        if self._ws is None:
            return
        deadline = time.monotonic() + seconds
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                frame = json.loads(self._ws.recv(timeout=remaining or 0.01))
            except TimeoutError:
                return
            except Exception:                       # noqa: BLE001 — socket closed
                return
            if "method" in frame:
                self._file(frame)
            if remaining <= 0:
                return

    # ------------------------------------------------------------ operations

    def navigate(self, url: str, settle_frames: int = SETTLE_FRAMES) -> dict:
        """Load a page and wait for it to actually draw."""
        self.start()
        self.errors = PageErrors()
        self._requests.clear()
        self.url = url
        self._call("Page.navigate", {"url": url}, session=True)
        self.drain(1.0)
        frames = self.wait_frames(settle_frames)
        return {"url": url, "frames": frames, "asked_frames": settle_frames,
                "errors": self.errors.to_dict()}

    def wait_frames(self, n: int = SETTLE_FRAMES) -> int:
        """Advance n animation frames; returns how many actually happened."""
        result = self._eval(_FRAMES_JS.format(n=n, ms=int(EVAL_TIMEOUT_S * 1000) // 2),
                            await_promise=True)
        return int(result) if isinstance(result, (int, float)) else 0

    def _eval(self, expression: str, await_promise: bool = False):
        result = self._call("Runtime.evaluate",
                            {"expression": expression, "returnByValue": True,
                             "awaitPromise": await_promise}, session=True)
        if "exceptionDetails" in result:
            detail = result["exceptionDetails"]
            raise BrowserUnavailable(
                detail.get("exception", {}).get("description") or detail.get("text", "eval failed"))
        return result.get("result", {}).get("value")

    def page_title(self) -> str:
        """What the loaded page calls itself — the cheapest identity check."""
        try:
            return str(self._eval("document.title") or "").strip()
        except BrowserUnavailable:
            return ""

    def probe(self) -> dict:
        """The cheap checks: is there a canvas, did it paint, how big is it."""
        self.drain()
        return self._eval(_PROBE_JS) or {"canvas": False, "count": 0}

    def liveness(self, frames: int = 30) -> dict:
        """Whether the picture changes over `frames` — a dead render loop's tell."""
        before = self.probe()
        advanced = self.wait_frames(frames)
        after = self.probe()
        return {"moving": _differs(before, after), "frames": advanced,
                "asked_frames": frames, "before": before, "after": after}

    def run_for(self, frames: int) -> dict:
        """Let the app run, and keep a picture of each end of the wait."""
        before = self.probe()
        shot_before = self._safe_shot(before)
        ran = self.wait_frames(frames)
        after = self.probe()
        return {"asked_frames": frames, "frames": ran, "changed": _differs(before, after),
                "stalled": ran < frames, "probe": after,
                "png_before": shot_before, "png_after": self._safe_shot(after)}

    def film(self, frames: int = 180, shots: int = 8,
             max_edge: int = MAX_SHOT_EDGE) -> dict:
        """A run of screenshots spread evenly over `frames` animation frames.

        One picture cannot show motion, and two show only its endpoints — a
        sprite that flickers, a character that moves and snaps back, a scroll
        that judders all look identical at either end. A short evenly-spaced
        run is the cheapest thing that shows *how* the screen got from one to
        the other.
        """
        shots = max(2, min(int(shots), 24))
        frames = max(shots, int(frames))
        clip = self.canvas_clip()          # one clip for all: frames that line up
        step = frames // (shots - 1)
        pngs = [self.screenshot(clip, max_edge=max_edge)]
        advanced = 0
        for _ in range(shots - 1):
            advanced += self.wait_frames(step)
            pngs.append(self.screenshot(clip, max_edge=max_edge))
        return {"pngs": pngs, "frames": advanced, "asked_frames": frames,
                "frames_between": step, "clipped": bool(clip)}

    def press(self, key: str, times: int = 1, settle_frames: int = PRESS_SETTLE_FRAMES,
              hold_frames: int = HOLD_FRAMES) -> dict:
        """Send a key the way a keyboard would, and report what came of it.

        The key is *held* for `hold_frames` before being released. A key that
        goes down and up between two frames is invisible to any game that polls
        `isDown` in its update loop, which is most of them — and the press then
        reports itself delivered, correctly, while nothing moves.

        Two separate questions are answered, because they have different fixes.
        *Delivered* is whether the page received the key at all — answered by a
        listener of our own, in the capture phase so the app cannot stop it.
        *Changed* is whether the picture then differed. A press that was
        delivered and changed nothing means the app ignored it; one that was
        never delivered is a browser problem. Reporting only "pressed" told the
        agent neither, and an agent with no evidence a keypress worked
        reasonably concludes it did not.
        """
        spec, code, key_code, text = key_spec(key)
        self._eval(_KEY_HOOK_JS)
        before = self.probe()
        # A picture of the screen either side of the press. Not for a model —
        # nothing is sent anywhere — but so "the picture changed" and "nothing
        # changed" are claims you can check rather than take on trust. A verdict
        # about pixels whose pixels nobody kept is not evidence of anything.
        shot_before = self._safe_shot(before)
        def event(kind: str) -> None:
            params = {"type": kind, "key": spec, "code": code,
                      "windowsVirtualKeyCode": key_code, "nativeVirtualKeyCode": key_code}
            if text and kind == "keyDown":
                params["text"] = text
            self._call("Input.dispatchKeyEvent", params, session=True)

        held = max(1, hold_frames)
        for index in range(max(1, times)):
            event("keyDown")
            self.wait_frames(held)             # the frames the game polls in
            event("keyUp")
            if index + 1 < max(1, times):
                self.wait_frames(2)            # a gap, so it reads as two presses
        # A screen that swaps on a keypress needs longer than the four frames
        # between presses to have drawn its new state.
        frames = self.wait_frames(settle_frames)
        seen = self._eval("window.__tranceKeys || []") or []
        after = self.probe()
        return {"key": key, "times": max(1, times), "delivered": [str(s) for s in seen],
                "changed": _differs(before, after), "frames": frames, "probe": after,
                "held_frames": held,
                "png_before": shot_before, "png_after": self._safe_shot(after)}

    def move_mouse(self, dx: float, dy: float, steps: int = 12,
                   settle_frames: int = PRESS_SETTLE_FRAMES) -> dict:
        """Sweep the mouse by (dx, dy) the way a hand would — many small moves.

        Free look in a pointer-lock game reads movementX/Y off mousemove
        events, so one jump from A to B is a single event that reads as a
        teleport; a sweep is what a camera can follow. Starts from wherever
        the last sweep ended (the viewport centre at first) and stays inside
        the viewport. Answers the same two questions as a keypress —
        *delivered*, *changed* — plus whether pointer lock was engaged while
        the mouse moved, because free look without lock is usually a game
        that never got the lock it asked for.
        """
        self._eval(_MOUSE_HOOK_JS)
        before = self.probe()
        shot_before = self._safe_shot(before)
        x, y = getattr(self, "_mouse", (self.width / 2.0, self.height / 2.0))
        steps = max(1, min(int(steps or 12), 60))
        for i in range(1, steps + 1):
            nx = min(max(x + dx * i / steps, 0.0), self.width - 1.0)
            ny = min(max(y + dy * i / steps, 0.0), self.height - 1.0)
            self._call("Input.dispatchMouseEvent",
                       {"type": "mouseMoved", "x": nx, "y": ny, "buttons": 0},
                       session=True)
            self.wait_frames(1)
        self._mouse = (min(max(x + dx, 0.0), self.width - 1.0),
                       min(max(y + dy, 0.0), self.height - 1.0))
        frames = self.wait_frames(settle_frames)
        seen = self._eval("window.__tranceMouse") or {}
        after = self.probe()
        return {"dx": dx, "dy": dy, "steps": steps,
                "delivered": int(seen.get("moves") or 0) > 0,
                "moves_seen": int(seen.get("moves") or 0),
                "movement": (round(float(seen.get("dx") or 0), 1),
                             round(float(seen.get("dy") or 0), 1)),
                "locked": bool(seen.get("locked")),
                "changed": _differs(before, after), "frames": frames,
                "probe": after,
                "png_before": shot_before, "png_after": self._safe_shot(after)}

    def click(self, x: float | None = None, y: float | None = None, text: str = "",
              settle_frames: int = PRESS_SETTLE_FRAMES) -> dict:
        """Click the page the way a mouse would.

        By the words on the thing, normally — "Join game", "Start" — because
        that is what the agent can see; coordinates are for canvas UIs, where
        the button is pixels. Same two questions as a keypress, because they
        have different fixes: *delivered* is whether the page received a click
        at all, *changed* is whether the picture then differed.
        """
        import json as _json

        target = {}
        if text:
            spot = self._eval(_FIND_CLICK_JS % _json.dumps(str(text))) or {}
            if not spot.get("found"):
                return {"found": False, "text": text,
                        "candidates": [str(c) for c in spot.get("candidates") or []]}
            x, y = spot["x"], spot["y"]
            target = {"tag": spot.get("tag", ""), "label": spot.get("label", "")}
        if x is None or y is None:
            raise ValueError("click needs the text on the thing, or x and y")

        self._eval(_CLICK_HOOK_JS)
        before = self.probe()
        shot_before = self._safe_shot(before)
        spot = {"x": float(x), "y": float(y), "button": "left", "clickCount": 1}
        # The move first: plenty of UIs arm on hover, and a click that lands
        # with no mouse ever having been near reads as untrusted input to some.
        self._call("Input.dispatchMouseEvent",
                   {"type": "mouseMoved", "x": float(x), "y": float(y)}, session=True)
        self._call("Input.dispatchMouseEvent", {"type": "mousePressed", **spot}, session=True)
        self._call("Input.dispatchMouseEvent", {"type": "mouseReleased", **spot}, session=True)
        frames = self.wait_frames(settle_frames)
        seen = self._eval("window.__tranceClicks || []") or []
        after = self.probe()
        return {"found": True, "x": round(float(x), 1), "y": round(float(y), 1), **target,
                "delivered": bool(seen), "changed": _differs(before, after),
                "frames": frames, "probe": after,
                "png_before": shot_before, "png_after": self._safe_shot(after)}

    def _safe_shot(self, probe: dict) -> bytes:
        """A screenshot for the record. Never raises: evidence is a nice-to-have
        and must not be the thing that ends a step.

        The whole page, not the canvas crop: interaction evidence exists to
        answer "did that click do anything", and the answer is often outside
        the canvas — measured on a tower-defense shop, clicking a DOM button
        highlighted the button and changed not one canvas pixel, so the
        before/after pair came out byte-identical and the click was reported
        as doing nothing. The canvas crop stays right for `look`, where the
        question is about the game image itself."""
        try:
            return self.screenshot(None)
        except Exception:                           # noqa: BLE001
            return b""

    def screenshot(self, clip: dict | None = None, max_edge: int = MAX_SHOT_EDGE) -> bytes:
        """A PNG of the page, or of just the rectangle given.

        Goes through the compositor, which is the whole reason this is the right
        call and `canvas.toDataURL()` is not: it captures what was actually
        rendered, so WebGL without preserveDrawingBuffer and canvases tainted by
        cross-origin images both come out correctly. Clipping to the canvas
        drops the page margins, and with them most of the image's token cost.

        `max_edge` shrinks anything bigger, in the browser, for free — a 4K
        canvas costs several times the tokens of a 768px one and shows a vision
        model nothing extra.
        """
        import base64

        params: dict = {"format": "png"}
        if clip:
            width, height = float(clip.get("width", 0)), float(clip.get("height", 0))
            if width >= 1 and height >= 1:
                scale = 1.0
                if max_edge and max(width, height) > max_edge:
                    scale = max_edge / max(width, height)
                params["clip"] = {"x": float(clip.get("x", 0)), "y": float(clip.get("y", 0)),
                                  "width": width, "height": height, "scale": scale}
        result = self._call("Page.captureScreenshot", params, session=True, timeout=60.0)
        return base64.b64decode(result["data"])

    def canvas_clip(self, probe: dict | None = None) -> dict | None:
        """The largest canvas's rectangle, for clipping a screenshot to it."""
        found = probe if probe is not None else self.probe()
        rect = found.get("rect") if found.get("canvas") else None
        return rect if rect and rect.get("width", 0) >= 1 else None
