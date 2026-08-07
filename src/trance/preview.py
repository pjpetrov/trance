"""Serving a project's web files, so you can actually open what was built.

Reading `index.html` tells you it exists. It does not tell you whether the game
runs, and that is the only question worth asking about a web app — so trance can
put a static server in front of the folder and hand you a URL.

It is a *separate* server on its own port rather than a path under trance's own,
because a real page asks for `/js/app.js` and `/style.css` from the root. Serving
it at `/preview/<session>/…` would 404 every one of those, and the page would
look broken for reasons that have nothing to do with the code.

It listens on every interface, so the page opens on your phone or on the laptop
you are actually sitting at, not only on the machine running trance. That is the
common case for a preview — a UI is worth looking at on a real screen. There is
no authentication on it, so it belongs on a network you trust, like the rest of
trance.

Deliberately narrow:

* Rooted at one directory, with every request resolved and refused if it escapes.
* Static files only — no CGI, no directory upload, no execution.
* Nothing is ever started on your behalf. A project with a build step is
  reported as such (`dev_command`) and still served as files; running `npm run
  dev` is your call, in your terminal, where you can see it.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from . import paths

#: Nothing here should be reachable from a served page.
HIDDEN = {".git", ".trance", "node_modules", "__pycache__", ".env"}


class _Handler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler, minus directory listings for private folders."""

    def translate_path(self, path: str) -> str:
        resolved = Path(super().translate_path(path)).resolve()
        root = Path(self.directory).resolve()
        # SimpleHTTPRequestHandler already refuses `..`, but it is the only
        # thing between a served page and the rest of the disk, so this checks
        # the result rather than trusting the sanitiser.
        if resolved != root and root not in resolved.parents:
            return str(root)
        if any(part in HIDDEN for part in resolved.relative_to(root).parts
               if resolved != root):
            return str(root / "__forbidden__")
        return str(resolved)

    def log_message(self, *args) -> None:
        """Quiet: the run console is the log that matters."""

    def end_headers(self) -> None:
        # A preview is looked at while it is being changed, so a cached copy of
        # the file you just fixed is worse than useless.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


@dataclass
class Preview:
    """A running static server for one directory."""

    root: str
    port: int
    server: object
    thread: object

    @property
    def url(self) -> str:
        """The address to hand someone on another machine, when there is one."""
        return f"http://{lan_address()}:{self.port}/"

    def at(self, host: str) -> str:
        """This preview as seen from `host` — whichever name got you to trance.

        Someone browsing trance at 192.168.1.5 cannot use a 127.0.0.1 link, and
        someone on the machine itself does not want the LAN address. The host
        the browser already used is right in both cases.
        """
        return f"http://{host or lan_address()}:{self.port}/"

    def to_dict(self) -> dict:
        return {"root": self.root, "port": self.port, "url": self.url,
                "local": f"http://localhost:{self.port}/"}

    def stop(self) -> None:
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:                     # already gone; nothing to do
            pass


def lan_address() -> str:
    """This machine's address on the network it can actually reach.

    Asked of the routing table rather than of DNS: `gethostname()` resolves to
    127.0.1.1 on a lot of Linux boxes, which is exactly the answer that does not
    work from another device. No packets are sent — a UDP connect only picks a
    route.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))            # TEST-NET-1, deliberately unroutable
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"                         # no network; local still works
    finally:
        probe.close()


def serve(directory: Path, host: str = "0.0.0.0", port: int = 0) -> Preview:  # noqa: S104
    """Start a static server for `directory` on every interface.

    `port` asks for a specific one. A preview that comes back on a new port every
    time breaks anything pointed at the old one — a tunnel, a link someone was
    sent, a tab left open — so a folder that has been served before is served
    again at the same address where that is still possible.
    """
    root = Path(directory).resolve()
    handler = partial(_Handler, directory=str(root))
    try:
        server = http.server.ThreadingHTTPServer((host, port), handler)
    except OSError:
        # Taken by something else in the meantime. A working preview on a new
        # port beats no preview on the old one.
        server = http.server.ThreadingHTTPServer((host, 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name=f"preview-{root.name}")
    thread.start()
    return Preview(root=str(root), port=server.server_address[1],
                   server=server, thread=thread)


#: A project with one of these has a build step: its index.html imports bare
#: module names ("three", "react") that only its dev server can resolve, so
#: serving the folder statically gives a page that loads and then fails.
BUILT_BY = ("vite.config.js", "vite.config.ts", "webpack.config.js",
            "next.config.js", "svelte.config.js", "rollup.config.js")
#: Scripts worth offering, best first.
DEV_SCRIPTS = ("dev", "start", "serve", "preview")


def dev_command(project: Path, folder: Path) -> dict | None:
    """The command that runs this app, when a static server will not do.

    Only ever reported, never run: a preview is a thing you click, and clicking
    it should not start a build on your machine. `needed` is the warning — this
    page will load from the static server and then fail, and here is why.

    Looks from the page's folder up to the project root: a Vite app keeps its
    package.json at the root and its index.html beside it, but plenty of layouts
    put the page a level or two down.
    """
    import json as _json

    project, folder = Path(project).resolve(), Path(folder).resolve()
    here = folder
    while True:
        manifest = here / "package.json"
        if manifest.is_file():
            try:
                data = _json.loads(manifest.read_text(encoding="utf8"))
            except (OSError, ValueError):
                data = {}
            scripts = data.get("scripts") or {}
            built = any((here / name).is_file() for name in BUILT_BY)
            for name in DEV_SCRIPTS:
                if name in scripts:
                    return {"dir": str(here), "script": name,
                            "command": f"npm run {name}",
                            "runs": scripts[name], "needed": built}
            return None
        if here == project or here.parent == here:
            return None
        here = here.parent


def web_root_for(project: Path, path: str) -> Path:
    """Where a page's own root is: the folder it lives in.

    A page in `server/public/` asks for `/js/app.js`, meaning
    `server/public/js/app.js`. Serving the project root instead would make every
    absolute path in the page a 404 — so the directory holding the file is the
    web root, and its siblings and subfolders come with it.
    """
    target = paths.inside(project, path)
    if target is None:                       # escapes the project: serve nothing else
        return Path(project).resolve()
    return target.parent if target.is_file() else target


#: `import * as THREE from 'three'` — a specifier that is not a path. Only a
#: bundler or an import map can say which file it means, so a static server
#: serves a page that loads and then dies on its first import.
_BARE = re.compile(
    r"""(?:^|[\s;{])(?:import|export)\s+(?:[^'"]*?\sfrom\s+)?['"]([^'"./][^'"]*)['"]""",
    re.MULTILINE)
_SCRIPTS = (".js", ".mjs", ".jsx", ".ts", ".tsx")
#: Build and test files import packages by name too, and always have. They are
#: not evidence about the page, so citing one as the reason would be wrong.
_NOT_THE_PAGE = ("test", "tests", "spec", "__tests__", "e2e", "scripts")


def bare_imports(folder: Path, limit: int = 3, max_files: int = 400) -> list[dict]:
    """Imports in this folder that a static server cannot resolve.

    The question "will this page work as files?" is answerable by looking, so
    it is looked at rather than guessed from the presence of a config file: a
    project with a vite.config.js and no bare imports serves perfectly well.
    """
    root, found, seen = Path(folder), [], 0
    for path in sorted(root.rglob("*")):
        if len(found) >= limit or seen >= max_files:
            break
        if path.suffix.lower() not in _SCRIPTS or not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if any(part in HIDDEN or part in _NOT_THE_PAGE for part in parts):
            continue
        if path.name.endswith(".config.js") or path.name.endswith(".config.ts"):
            continue
        seen += 1
        try:
            text = path.read_text(encoding="utf8", errors="ignore")
        except OSError:
            continue
        for match in _BARE.finditer(text):
            found.append({"file": path.relative_to(root).as_posix(),
                          "specifier": match.group(1),
                          "line": text[:match.start()].count("\n") + 1})
            break                      # one example per file is enough to explain
    return found[:limit]


#: Where the ngrok agent describes what it is currently tunnelling. It is a
#: local API on a fixed port, so asking is cheap and needs no credentials.
#: Overridable for anyone whose agent runs somewhere else.
NGROK_API = os.environ.get("TRANCE_NGROK_API", "http://127.0.0.1:4040/api/tunnels")


def public_url(port: int, api: str = NGROK_API, timeout: float = 0.4) -> str:
    """The public URL for a preview, if something is tunnelling that port.

    trance does not start the tunnel — that stays a decision you make in a
    terminal — but it can see one that is running, and a share link you have to
    go and find in another window is a share link you will not use.

    Nothing is inferred from the tunnel being up: the port has to match, so a
    tunnel pointed at something else is not offered as this page's link.
    """
    try:
        with urllib.request.urlopen(api, timeout=timeout) as response:
            tunnels = json.load(response).get("tunnels") or []
    except (urllib.error.URLError, OSError, ValueError):
        return ""                      # no agent running, which is the normal case

    https, plain = "", ""
    for tunnel in tunnels:
        addr = (tunnel.get("config") or {}).get("addr") or ""
        if not addr.rstrip("/").endswith(f":{port}"):
            continue
        url = tunnel.get("public_url") or ""
        if url.startswith("https://"):
            https = https or url
        else:
            plain = plain or url
    return https or plain              # https for preference; both are offered


class NoTunnelTool(RuntimeError):
    """ngrok is not installed, or not on the PATH trance was started with."""


@dataclass
class Tunnel:
    """An ngrok agent trance started, and is therefore responsible for."""

    port: int
    url: str
    proc: object
    #: What it was started with, so the UI can say whether it has a password.
    policy: str = ""
    #: Someone else's agent, which trance found and used rather than started.
    adopted: bool = False
    #: Managed through that agent's API rather than by running our own ngrok.
    via_agent: bool = False

    @property
    def running(self) -> bool:
        if self.adopted or self.via_agent:
            return bool(agent_tunnels())     # theirs; ask the agent, do not assume
        return self.proc is not None and self.proc.poll() is None

    def to_dict(self) -> dict:
        return {"url": self.url, "port": self.port,
                "adopted": self.adopted and not self.via_agent,
                "protected": bool(self.policy), "running": self.running}

    def stop(self) -> None:
        # Ours through the agent's API: take the tunnel down and leave the agent
        # — the process belongs to whoever started it.
        if self.via_agent:
            try:
                _agent("/" + AGENT_TUNNEL, method="DELETE")
            except (urllib.error.URLError, OSError):
                pass
            return
        # Nothing to stop for a tunnel we adopted rather than started: it
        # belongs to whoever ran ngrok, and killing it would be a surprise.
        if self.proc is None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self.proc.kill()
            except Exception:
                pass


def agent_tunnels(api: str = NGROK_API, timeout: float = 0.4) -> list[dict]:
    """What the running ngrok agent is tunnelling, if one is running at all."""
    try:
        with urllib.request.urlopen(api, timeout=timeout) as response:
            return json.load(response).get("tunnels") or []
    except (urllib.error.URLError, OSError, ValueError):
        return []


#: The name trance gives the tunnels it manages through the agent API.
AGENT_TUNNEL = "trance-preview"


def agent_running(api: str = NGROK_API, timeout: float = 0.4) -> bool:
    """Is an ngrok agent running at all — tunnels or not?

    Not the same question as `agent_tunnels()`. An agent whose tunnels have all
    been closed still holds the one session a free account gets, so it still
    answers its API and still stops a second agent from starting. Asking the
    wrong one of these two questions spawns an ngrok that cannot connect.
    """
    try:
        with urllib.request.urlopen(api, timeout=timeout) as response:
            json.load(response)
            return True
    except urllib.error.HTTPError:
        return True                        # answering, however unhappily
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _agent(path: str = "", method: str = "GET", body: dict | None = None,
           api: str = NGROK_API, timeout: float = 15.0):
    """Talk to the local ngrok agent's own API."""
    request = urllib.request.Request(
        api + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"content-type": "application/json"} if body is not None else {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


def retarget_agent(port: int, policy: str = "") -> str:
    """Point the running agent at `port`, replacing whatever it was serving.

    Adding a second tunnel does not work on a free account: both claim the same
    public URL and it starts answering 502. Replacing does — same agent session,
    same URL, and no process of anyone else's is killed. Which also means the
    link keeps working when you move from one preview to another.
    """
    for existing in agent_tunnels():
        name = existing.get("name")
        if name:
            try:
                _agent("/" + urllib.parse.quote(name, safe=""), method="DELETE")
            except (urllib.error.URLError, OSError):
                pass                      # already gone, or the agent stopped

    body: dict = {"addr": str(port), "proto": "http", "name": AGENT_TUNNEL}
    if policy:
        body["traffic_policy"] = Path(policy).read_text(encoding="utf8")
    added = _agent(method="POST", body=body)
    return added.get("public_url") or ""


class TunnelBusy(RuntimeError):
    """An ngrok agent is already running, and the account allows only one."""


def start_tunnel(port: int, policy: str = "", wait_s: float = 25.0) -> Tunnel:
    """Run `ngrok http <port>` and wait for it to report its public URL.

    The URL is read back from the agent's own API rather than parsed out of its
    output, because that is the thing the agent considers true — and it is the
    same source used when someone starts ngrok themselves, so a tunnel behaves
    identically whichever way it was started.

    An agent that is already running is dealt with first. Free ngrok accounts
    allow exactly one at a time, so starting a second gets ERR_NGROK_334 — and
    "502" is a poor way to learn that the tunnel you started an hour ago is
    still up.
    """
    running = agent_tunnels()
    if not running and agent_running():
        # An agent with no tunnels: still holds the session, still blocks a
        # second one. Hand it the tunnel instead of starting a rival.
        url = retarget_agent(port, policy)
        if url:
            return Tunnel(port=port, url=url, proc=None, policy=policy,
                          adopted=True, via_agent=True)
    if running:
        mine = [t for t in running
                if (t.get("config") or {}).get("addr", "").rstrip("/").endswith(f":{port}")]
        if mine:
            # Already serving this exact port: adopt it rather than fight it.
            https = next((t["public_url"] for t in mine
                          if t.get("public_url", "").startswith("https://")), "")
            return Tunnel(port=port, url=https or mine[0]["public_url"],
                          proc=None, policy=policy, adopted=True)
        # Pointing at something else. One agent at a time is all a free account
        # allows, so rather than refuse, reuse: the agent is told to serve this
        # port instead. Its URL does not even change.
        url = retarget_agent(port, policy)
        if url:
            return Tunnel(port=port, url=url, proc=None, policy=policy,
                          adopted=True, via_agent=True)
        elsewhere = ", ".join(sorted({(t.get("config") or {}).get("addr", "?")
                                      for t in running}))
        raise TunnelBusy(
            f"An ngrok agent is already tunnelling {elsewhere} and would not give "
            f"up its tunnel. Stop it and try again.")

    binary = shutil.which("ngrok")
    if not binary:
        raise NoTunnelTool(
            "ngrok is not on trance's PATH. Install it (~/.local/bin is fine) and "
            "restart trance, or start the tunnel yourself with tools/preview-tunnel.sh.")

    command = [binary, "http", str(port)]
    if policy:
        command += ["--traffic-policy-file", policy]
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, start_new_session=True)

    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output = (proc.stdout.read() if proc.stdout else "") or ""
            if "ERR_NGROK_334" in output or "ERR_NGROK_108" in output:
                raise TunnelBusy(
                    "An ngrok agent is already running, and one at a time is all a "
                    "free ngrok account allows. Stop the other one and try again.")
            raise RuntimeError(_ngrok_failure(output))
        url = public_url(port)
        if url:
            return Tunnel(port=port, url=url, proc=proc, policy=policy)
        time.sleep(0.3)

    proc.kill()
    raise RuntimeError("ngrok did not report a public URL within 25s.")


def _ngrok_failure(output: str) -> str:
    """ngrok's own words, which say what to do; ours only if it said nothing."""
    for line in output.splitlines():
        if "ERR_NGROK" in line or "authentication failed" in line.lower():
            return line.strip().removeprefix("ERROR:").strip()
    return (output.strip().splitlines() or
            ["ngrok exited immediately. Has it been given an authtoken? "
             "`ngrok config add-authtoken <token>`"])[-1][:300]
