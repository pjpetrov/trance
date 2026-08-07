"""Serving a project's web files, so you can actually open what was built.

Reading `index.html` tells you it exists. It does not tell you whether the game
runs, and that is the only question worth asking about a web app — so trance can
put a static server in front of the folder and hand you a URL.

It is a *separate* server on its own port rather than a path under trance's own,
because a real page asks for `/js/app.js` and `/style.css` from the root. Serving
it at `/preview/<session>/…` would 404 every one of those, and the page would
look broken for reasons that have nothing to do with the code.

Deliberately narrow:

* Bound to 127.0.0.1. This exists to open a page on the machine you are sitting
  at, not to publish anything.
* Rooted at one directory, with every request resolved and refused if it escapes.
* Static files only — no CGI, no directory upload, no execution.
"""

from __future__ import annotations

import http.server
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

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
        return f"http://127.0.0.1:{self.port}/"

    def to_dict(self) -> dict:
        return {"root": self.root, "port": self.port, "url": self.url}

    def stop(self) -> None:
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:                     # already gone; nothing to do
            pass


def serve(directory: Path) -> Preview:
    """Start a static server for `directory` on a free local port."""
    root = Path(directory).resolve()
    handler = partial(_Handler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
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
    """The command that actually runs this app, if a static server will not.

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
    target = (Path(project) / path).resolve()
    return target.parent if target.is_file() else target


#: Where a dev server announces itself. Vite, CRA, Next and http-server all
#: print a localhost URL; reading it back beats guessing a port.
_URL = re.compile(r"https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?/?\S*")


@dataclass
class DevServer:
    """A project's own dev server, run because a static one will not do."""

    root: str
    command: str
    proc: object
    lines: list = field(default_factory=list)
    url: str = ""

    def to_dict(self) -> dict:
        return {"root": self.root, "command": self.command, "url": self.url,
                "running": self.running, "output": "".join(self.lines[-60:])}

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        """Kill the whole group: npm spawns the real server as a child."""
        if self.proc is None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self.proc.kill()
            except Exception:
                pass


def start_dev(directory: Path, command: str, wait_s: float = 20.0) -> DevServer:
    """Run a dev command and wait for it to say where it is listening.

    Nothing is guessed: the URL comes from the server's own output, so a Vite
    that picked 5174 because 5173 was taken still opens correctly.
    """
    proc = subprocess.Popen(
        ["bash", "-lc", command], cwd=str(directory),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, start_new_session=True,
    )
    server = DevServer(root=str(directory), command=command, proc=proc)

    def pump() -> None:
        for line in proc.stdout:                      # closes when it exits
            server.lines.append(line)
            if not server.url:
                found = _URL.search(line)
                if found:
                    server.url = found.group(0).rstrip(".,")
            del server.lines[:-400]

    threading.Thread(target=pump, daemon=True, name="devserver-out").start()

    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if server.url or not server.running:
            break
        time.sleep(0.1)
    return server
