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

    def alive(self) -> bool:
        """In-process, so alive as long as its thread is."""
        return getattr(self.thread, "is_alive", lambda: False)()

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


#: A dev server announces itself on stdout and then serves forever. These are
#: the shapes vite, next, webpack and http-server print; the port is the only
#: part trance needs, since the host it binds is not necessarily reachable.
_ANNOUNCED = re.compile(r"https?://(?:[\w.\-\[\]]+):(\d{2,5})")


@dataclass
class DevServer:
    """A dev command someone asked for, running until it is stopped.

    Not a static server: this is the project's own tooling, with its own build
    step, its own port and its own opinions about which hosts may reach it. All
    trance does is start it where it was told to, watch its output for the port
    it settled on, and keep the handle so it can be stopped again.
    """

    command: str
    root: str
    port: int
    process: object
    log: str
    #: The group leader's pid — what a handle re-adopted after a harness
    #: restart has instead of a Popen. The dev server runs in its own session,
    #: so it survives trance dying; this is how the next trance finds it.
    pid: int = 0
    #: What the launcher wants said about how this server came up — a half-up
    #: stack, a config line that must follow. Empty usually.
    note: str = ""
    #: The PORT this run's backend was told to use. Ephemeral by design: the
    #: map for a run is the run's process handle, and freed ports free with it.
    env_port: int = 0

    def __post_init__(self) -> None:
        if self.process is not None and not self.pid:
            self.pid = getattr(self.process, "pid", 0) or 0

    @property
    def url(self) -> str:
        return f"http://{lan_address()}:{self.port}/"

    def at(self, host: str) -> str:
        return f"http://{host or lan_address()}:{self.port}/"

    def alive(self) -> bool:
        if self.process is not None:
            return self.process.poll() is None
        if not self.pid:
            return False
        try:
            os.kill(self.pid, 0)
            return True
        except PermissionError:
            return True                    # exists, not ours to signal
        except (ProcessLookupError, OSError):
            return False

    def output(self, lines: int = 40) -> str:
        try:
            return "\n".join(Path(self.log).read_text(encoding="utf8",
                                                      errors="replace").splitlines()[-lines:])
        except OSError:
            return ""

    def to_dict(self) -> dict:
        return {"root": self.root, "port": self.port, "url": self.url,
                "local": f"http://localhost:{self.port}/",
                "command": self.command, "dev": True, "alive": self.alive()}

    def stop(self) -> None:
        """Kill the whole group. A dev server is a tree — npm spawns node, node
        spawns esbuild — and killing only the one trance started leaves the
        rest holding the port."""
        pid = getattr(self.process, "pid", 0) or self.pid
        if not pid:
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            if self.process is not None:
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
            else:
                # Re-adopted: nothing to wait on, so give it a moment and
                # finish the job if it ignored the polite signal.
                for _ in range(50):
                    if not self.alive():
                        break
                    time.sleep(0.1)
                else:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def adopt_dev(record: dict) -> DevServer | None:
    """A handle to a dev server a previous trance started, if it still runs.

    The dev server outlives the harness by design — it runs in its own session
    — so a restart used to leave it running *and* forgotten: holding its port,
    absent from the UI, with no button anywhere that could stop it.
    """
    pid = int(record.get("pid") or 0)
    server = DevServer(command=str(record.get("command") or ""),
                       root=str(record.get("root") or ""),
                       port=int(record.get("port") or 0),
                       process=None, log=str(record.get("log") or ""), pid=pid)
    return server if pid and server.port and server.alive() else None


class DevServerFailed(RuntimeError):
    """Started, and stopped or never announced a port. Carries what it said."""

    def __init__(self, message: str, output: str = ""):
        super().__init__(message)
        self.output = output


#: What EADDRINUSE looks like in a node server's dying words. The port is the
#: capture: knowing *which* port was squatted is what makes the retry and the
#: config warning possible.
_PORT_TAKEN = re.compile(r"EADDRINUSE[^\d]*(\d{2,5})")


def free_port() -> int:
    """A port the OS says is free right now."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def run_dev(directory: Path, command: str, *, wait_s: float = 90.0,
            log_dir: Path | None = None) -> DevServer:
    """Start a dev command with a fresh free PORT injected, and wait for it to
    say where it is.

    The user's design, after the layered version proved too complicated: find
    a free port, hand it to the backend as PORT, let the frontend dev server
    find its own and announce it — the announcement is parsed either way. The
    map lives only as this process handle; when the run ends the processes are
    killed and the ports free themselves, and the next run allocates fresh.

    A backend that reads PORT can therefore never collide with a squatter. One
    that hard-codes its port still can, and no retry fixes hard-coding — so
    the failure modes are *said* instead: a child that died of EADDRINUSE
    behind a successful announcement, or a config whose proxy targets a number
    this run is not on.
    """
    chosen = free_port()
    # HOST for the servers that read it (Create React App, some node
    # frameworks): trance is browsed from other machines, and a dev server
    # that binds localhost is unreachable from every one of them. The
    # orchestrator adds the per-tool flag for those that don't read it; this
    # covers the rest without a flag anyone has to remember.
    server = _launch_dev(directory, command, wait_s=wait_s, log_dir=log_dir,
                         env_extra={"PORT": str(chosen), "HOST": "0.0.0.0"})
    server.env_port = chosen

    notes = []
    try:
        said = Path(server.log).read_text(encoding="utf8", errors="replace")
    except OSError:
        said = ""
    died = _PORT_TAKEN.search(said)
    if died:
        # concurrently keeps running when one child dies, so vite announces
        # and the launch looks healthy while the backend is a corpse — the
        # exact shape that produced an evening of websocket errors.
        notes.append(
            f"WARNING: part of this stack died of EADDRINUSE on port "
            f"{died.group(1)} even though the dev server came up — the app is "
            f"half up. Port {died.group(1)} is hard-coded somewhere; a server "
            f"that reads process.env.PORT would have started on {chosen}.")
    config = _vite_config(Path(directory))
    if config is not None:
        text = config.read_text(encoding="utf8", errors="replace")
        strangers = sorted({p for p in re.findall(r"localhost:(\d{2,5})", text)
                            if int(p) not in (chosen, server.port)})
        if strangers:
            notes.append(
                f"NOTE: {config.name} targets localhost:{', localhost:'.join(strangers)} "
                f"but this run's backend was started with PORT={chosen} — make the "
                f"target read process.env.PORT so it follows.")
    server.note = " ".join(notes)
    return server


def _launch_dev(directory: Path, command: str, *, wait_s: float,
                log_dir: Path | None, env_extra: dict | None = None) -> DevServer:
    directory = Path(directory).resolve()
    log_dir = Path(log_dir) if log_dir else directory
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / "dev-server.log"

    handle = open(log, "w", encoding="utf8")                      # noqa: SIM115
    env = {**os.environ, **(env_extra or {})} if env_extra else None
    process = subprocess.Popen(                                   # noqa: S602
        command, shell=True, cwd=str(directory),
        stdout=handle, stderr=subprocess.STDOUT,
        start_new_session=True, text=True, env=env,
    )

    started = time.time()
    while time.time() - started < wait_s:
        if process.poll() is not None:
            handle.close()
            raise DevServerFailed(
                f"`{command}` exited before it served anything "
                f"(status {process.returncode}).",
                _tail(log))
        try:
            said = log.read_text(encoding="utf8", errors="replace")
        except OSError:
            said = ""
        found = _ANNOUNCED.search(said)
        if found:
            handle.close()
            return DevServer(command=command, root=str(directory),
                             port=int(found.group(1)), process=process, log=str(log))
        time.sleep(0.25)

    # Still running, still silent. Killing it is the honest end: a server whose
    # port nobody knows is a process nobody can reach and nobody will reap.
    output = _tail(log)
    handle.close()
    DevServer(command, str(directory), 0, process, str(log)).stop()
    raise DevServerFailed(
        f"`{command}` did not print an address within {int(wait_s)}s, so there is "
        f"nothing to open. It has been stopped.", output)


def _tail(log: Path, lines: int = 30) -> str:
    try:
        return "\n".join(log.read_text(encoding="utf8",
                                       errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


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


def _vite_config(root: Path) -> Path | None:
    """The Vite config governing `root` — beside it, or up to two levels up.

    Beside is the usual place; up is real too: a workspace keeps one config at
    the repo root and points the frontend at it with `--config ../vite.config.ts`.
    """
    here = Path(root)
    for _ in range(3):
        found = next((here / name for name in ("vite.config.js", "vite.config.ts",
                                               "vite.config.mjs")
                      if (here / name).is_file()), None)
        if found is not None:
            return found
        if here.parent == here:
            return None
        here = here.parent
    return None


def allow_host(root: Path, public_url: str) -> dict:
    """Put the tunnel's host into the Vite config, mechanically.

    The note below told the user which line to paste; every share against a
    dev server then failed once with "Blocked request" while they went and
    pasted it. A one-line edit with a known shape does not need a person — or
    an agent, which would spend a model call and a step's ceremony on it.
    Vite watches its own config and restarts, so the edit takes effect alone.

    Returns {"edited": bool, "file": str, "note": str}.
    """
    config = _vite_config(Path(root))
    if config is None or not public_url:
        return {"edited": False, "file": "", "note": ""}
    host = public_url.split("://", 1)[-1].split("/", 1)[0]
    try:
        text = config.read_text(encoding="utf8")
    except OSError:
        return {"edited": False, "file": str(config), "note": ""}

    if re.search(r"allowedHosts\s*:\s*true", text):
        return {"edited": False, "file": str(config),
                "note": "the config already allows every host"}
    listed = re.search(r"allowedHosts\s*:\s*\[([^\]]*)\]", text)
    if listed:
        if host in listed.group(1):
            return {"edited": False, "file": str(config),
                    "note": f"{host} is already allowed"}
        opening = listed.start(1)
        edited = text[:opening] + f"'{host}', " + text[opening:]
    else:
        server = re.search(r"server\s*:\s*\{", text)
        if server:
            at = server.end()
            edited = text[:at] + f" allowedHosts: ['{host}']," + text[at:]
        else:
            block = re.search(r"(defineConfig\s*\(\s*\{|export\s+default\s+\{)", text)
            if not block:
                return {"edited": False, "file": str(config), "note": ""}
            at = block.end()
            edited = text[:at] + f"\n  server: {{ allowedHosts: ['{host}'] }},"  + text[at:]
    try:
        config.write_text(edited, encoding="utf8")
    except OSError:
        return {"edited": False, "file": str(config), "note": ""}
    return {"edited": True, "file": config.name,
            "note": (f"added {host} to server.allowedHosts in {config.name} — "
                     f"Vite restarts itself on config changes, so the link works now")}


def allowed_hosts_note(root: Path, public_url: str) -> str:
    """What a Vite dev server needs before it will answer a tunnel.

    Vite refuses requests whose Host header it does not recognise, so a tunnel
    to a dev server returns "Blocked request" rather than the app — an error
    about the config that reads like a broken tunnel. Only worth saying when
    there is a Vite config to say it about.
    """
    config = _vite_config(Path(root))
    if config is None or not public_url:
        return ""
    host = public_url.split("://", 1)[-1].split("/", 1)[0]
    return (f"Vite will answer this tunnel only once its host is allowed. In "
            f"{config.name}, under `server`, add:\n\n"
            f"    allowedHosts: ['{host}']\n\n"
            f"(or `allowedHosts: true` while you are testing). Without it the "
            f"page returns \"Blocked request\" and the tunnel looks broken when it "
            f"is not.")


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
