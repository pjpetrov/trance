"""One way to read a path that came from outside trance.

A model writes `/src/game/scene.js` about as often as `src/game/scene.js`. It
means the same file both times — the leading slash is how paths look in the HTML
it is editing — but `Path("/project") / "/src/game/scene.js"` is
`/src/game/scene.js`, because joining an absolute path throws the left side
away. So the read 404s, the write is refused as "outside the project directory",
and `get_definition` reports no such symbol. Three different messages for one
harmless habit, none of which says what to do about it.

The same goes for the dot segments a model copies out of an import: `src/./game`
and `src/game` are the same directory to everyone except an exact string match,
which is what the symbol index does.

So every path that arrives from a model, from the browser, or from an HTTP
request is put through `relative()` first. What it is *not* allowed to do is
paper over an escape: `../` stays `../` and is still refused by `inside()`.
"""

from __future__ import annotations

import posixpath
from pathlib import Path, PurePosixPath


def relative(root: Path | str, path: str) -> str:
    """`path` as a clean project-relative posix path, however it was written.

    Absolute paths that really are inside the project keep their meaning; ones
    that are not are read as project-relative, since that is what a leading
    slash almost always means coming from a model.
    """
    text = (path or "").strip().replace("\\", "/")
    if not text:
        return ""

    if text.startswith("/"):
        here, base = PurePosixPath(text), PurePosixPath(str(Path(root)))
        text = (str(here.relative_to(base)) if here == base or base in here.parents
                else text.lstrip("/"))

    cleaned = posixpath.normpath(text)
    return "" if cleaned == "." else cleaned


def inside(root: Path | str, path: str) -> Path | None:
    """Resolve `path` under `root`, or None if it escapes the project.

    Normalising first and checking after is deliberate: the check is on the
    resolved result, so no amount of `..` or symlinking gets past it.
    """
    base = Path(root).resolve()
    target = (base / relative(base, path)).resolve()
    if target != base and base not in target.parents:
        return None
    return target
