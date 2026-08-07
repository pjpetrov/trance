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
import threading
from dataclasses import dataclass
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


def web_root_for(project: Path, path: str) -> Path:
    """Where a page's own root is: the folder it lives in.

    A page in `server/public/` asks for `/js/app.js`, meaning
    `server/public/js/app.js`. Serving the project root instead would make every
    absolute path in the page a 404 — so the directory holding the file is the
    web root, and its siblings and subfolders come with it.
    """
    target = (Path(project) / path).resolve()
    return target.parent if target.is_file() else target
