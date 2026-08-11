"""One browser, one static server, for the length of a step.

Both are held open across tool calls rather than made per call, and that is the
design, not an optimisation. A game that needs a keypress to start has to still
be running when the screenshot is taken — measured on a real project, the
title screen and the running game share almost no pixels, so a fresh browser per
call would only ever photograph the menu and judge that.

Nothing here talks to a model. It produces pictures and numbers; deciding what
they mean is the caller's job, and the vision call lives in trance/vision.py.
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import imagediff, preview
from ..browser import Browser, BrowserUnavailable, MAX_SHOT_EDGE, find_chrome

#: Where a project's page usually is, best first. Only used when the agent does
#: not name one.
PAGE_CANDIDATES = ("index.html", "public/index.html", "dist/index.html",
                   "build/index.html", "src/index.html", "www/index.html")

#: Screenshots go here, under the project's own working directory, so they sit
#: beside the rest of trance's state and are ignored by the project's git.
SHOTS_DIR = ".trance/shots"

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def available() -> bool:
    """Whether this machine can run visual checks at all."""
    return find_chrome() is not None


def default_page(project: Path) -> str:
    """The page to open when the task did not say. Empty if there is no obvious one."""
    root = Path(project)
    for candidate in PAGE_CANDIDATES:
        if (root / candidate).is_file():
            return candidate
    found = sorted(root.glob("*/index.html")) or sorted(root.glob("*/*/index.html"))
    for page in found:
        parts = set(page.relative_to(root).parts)
        if not parts & {"node_modules", ".trance", ".git", "tests", "test"}:
            return str(page.relative_to(root))
    return ""


_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _title_of(page: Path) -> str:
    """The <title> a page file gives itself, or "" when it has none."""
    try:
        found = _TITLE.search(page.read_text(encoding="utf8", errors="replace"))
    except OSError:
        return ""
    return found.group(1).strip() if found else ""


class VisualSession:
    """The browser side of a step: serve the project, drive it, photograph it."""

    def __init__(self, project: Path, *, session_id: str = "", step_id: str = "",
                 cancel_token: str = ""):
        self.project = Path(project).resolve()
        self.session_id = session_id
        self.step_id = step_id
        self.cancel_token = cancel_token or session_id
        self._browser: Browser | None = None
        self._preview = None
        self._dev = None
        #: The dev command behind the page, when one is running. Reported so
        #: the agent knows what it is looking at.
        self.dev_command = ""
        self._shots = 0
        self.page = ""

    # ------------------------------------------------------------- lifecycle

    @property
    def browser(self) -> Browser:
        if self._browser is None:
            self._browser = Browser(cancel_token=self.cancel_token)
            self._browser.start()
        return self._browser

    def _serve(self, page: str) -> str:
        """A URL for `page` — the project's own dev server when it needs one,
        else a static server rooted where the page lives.

        A Vite app served statically loads and then dies on its first bare
        import, so a visual test of one could only ever photograph the failure.
        The dev script comes from the project's own package.json — nothing is
        invented — it is started once per step, its output goes to
        .trance/dev-server.log, and it is stopped with the step. It also
        sidesteps a broken `npm run build`: a dev server transpiles without
        typechecking, so the app runs and can be judged while the type errors
        are still someone's open task.
        """
        root = preview.web_root_for(self.project, page)
        wants = preview.dev_command(self.project, root)
        if wants and wants.get("needed"):
            if self._dev is not None and not self._dev.alive():
                self._dev = None
            if self._dev is None:
                try:
                    self._dev = preview.run_dev(
                        Path(wants["dir"]), wants["command"],
                        log_dir=self.project / ".trance")
                except preview.DevServerFailed as failed:
                    raise BrowserUnavailable(
                        f"this project needs its dev server ({wants['command']}) and "
                        f"it would not start: {failed}. Its last words:\n"
                        f"{failed.output[-800:]}") from failed
                self.dev_command = wants["command"]
            name = Path(page).name
            path = "" if name == "index.html" else name
            return f"http://127.0.0.1:{self._dev.port}/{path}"
        if self._preview is None or Path(self._preview.root) != root:
            if self._preview is not None:
                self._preview.stop()
            self._preview = preview.serve(root, host="127.0.0.1", port=0)
        name = Path(page).name if (self.project / page).is_file() else ""
        return f"http://127.0.0.1:{self._preview.port}/{name}"

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._preview is not None:
            self._preview.stop()
            self._preview = None
        if self._dev is not None:
            # The step started it, the step ends it: a dev server left behind
            # holds its port against every later step, and nothing else reaps it.
            self._dev.stop()
            self._dev = None
            self.dev_command = ""

    # ------------------------------------------------------------ operations

    def open(self, path: str = "", settle_frames: int = 60) -> dict:
        """Serve the project, load the page, and report what it did on arrival."""
        page = (path or "").strip().lstrip("/") or default_page(self.project)
        if not page:
            raise BrowserUnavailable(
                "no page to open: this project has no index.html that I could find. "
                "Pass `path` with the HTML file to load, relative to the project root.")
        if not (self.project / page).exists():
            raise BrowserUnavailable(f"{page} does not exist in this project")
        self.page = page
        url = self._serve(page)
        loaded = self.browser.navigate(url, settle_frames=settle_frames)
        probe = self.browser.probe()
        # Is what answered actually this project? Watched live: a Docker
        # container squatted the expected port and answered 200 with its own
        # page, and two agents spent an hour debugging a chatbot's HTML as if
        # it were their game. The page's own title against the project's is
        # the cheap mechanical version of "check the endpoint is our app".
        expected = _title_of(self.project / page)
        actual = self.browser.page_title()
        wrong_app = bool(expected and actual
                         and expected.lower() not in actual.lower()
                         and actual.lower() not in expected.lower())
        return {
            "page": page, "url": url,
            "frames": loaded["frames"], "asked_frames": loaded["asked_frames"],
            "errors": loaded["errors"], "probe": probe,
            # Running behind the page, when the project needs one. The old
            # answer — serve it statically and warn that what you see may be
            # the failure — meant a Vite project could not be visually tested
            # at all.
            "dev_server": self.dev_command,
            "dev_note": self._dev.note if self._dev is not None else "",
            "title": actual, "expected_title": expected, "wrong_app": wrong_app,
            "needs_build": False,
            "build_command": "",
        }

    def _needs_a_page(self) -> None:
        """Refuse to inspect a browser that was never sent anywhere.

        Every tool here starts the browser lazily, so calling one before
        open_page leaves it on about:blank — and about:blank is a white page
        that never changes. Which is exactly what it reported: "the screen did
        not change at all", true and useless, next to two blank screenshots. A
        confident wrong answer is worse than a refusal that says what to do.
        """
        if not self.page:
            raise BrowserUnavailable(
                "no page is open. Call open_page first — everything here looks at "
                "the page the browser is on, and until then that is a blank one.")

    def press(self, key: str, times: int = 1, hold_frames: int | None = None) -> dict:
        self._needs_a_page()
        return self._with_shots(self.browser.press(
            key, times=times,
            **({"hold_frames": hold_frames} if hold_frames else {})))

    def click(self, text: str = "", x: float | None = None,
              y: float | None = None) -> dict:
        self._needs_a_page()
        result = self.browser.click(x=x, y=y, text=text)
        if not result.get("found", True):
            return result                  # nothing happened; nothing to compare
        return self._with_shots(result)

    def wait(self, frames: int = 120) -> dict:
        """Let the app run before judging it.

        Needed because a screen is not finished the moment it appears: measured
        on a real game, four ghosts leave their pen over the two seconds after
        the start key, so a screenshot taken straight after the press shows one
        and reads as a bug. Counted in animation frames rather than seconds so
        it means the same thing on a slow machine.
        """
        self._needs_a_page()
        found = self._with_shots(self.browser.run_for(frames))
        found["errors"] = self.browser.errors.to_dict()
        return found

    def _with_shots(self, result: dict) -> dict:
        """Save the before/after pictures and compare them properly.

        The screenshots decide, not the canvas readback. Asking the canvas
        whether it changed is a different question from whether the screen did,
        and they disagree exactly where it hurts: a WebGL canvas reads back
        empty, so its hash is a constant and a moving game reports "nothing
        changed" for ever. Anything composited but not on the canvas — a DOM
        score, an overlay — is invisible to the readback too.
        """
        pngs = {}
        for side in ("before", "after"):
            png = result.pop(f"png_{side}", b"")
            pngs[side] = png
            result[f"shot_{side}"] = self.save(png) if png else ""

        if pngs["before"] and pngs["after"]:
            diff = imagediff.compare(pngs["before"], pngs["after"])
            result["diff"] = {"identical": diff.identical, "differing": diff.differing,
                              "total": diff.total, "fraction": diff.fraction,
                              "how": diff.how, "note": diff.note,
                              "described": diff.describe()}
            result["changed"] = not diff.identical
        return result

    def check(self, frames: int = 30) -> dict:
        """The free checks: did it paint, is it still painting, did it complain."""
        self._needs_a_page()
        self.browser.drain()
        live = self.browser.liveness(frames)
        probe = live["after"]
        return {
            "canvas": probe.get("canvas", False),
            "canvases": probe.get("count", 0),
            "size": (f"{probe.get('w')}x{probe.get('h')}" if probe.get("canvas") else ""),
            "blank": probe.get("uniform"),
            "moving": live["moving"],
            "frames": live["frames"],
            "read_via": probe.get("how"),
            "note": probe.get("note"),
            "errors": self.browser.errors.to_dict(),
        }

    def film(self, frames: int = 180, shots: int = 8) -> dict:
        """A burst of screenshots over a span of time, saved where the UI reads.

        Returns the saved paths, the raw PNGs (for a vision model), and how
        much of the screen changed between consecutive pictures — measured, so
        a caller with no vision model still learns whether anything moved and
        when it stopped.
        """
        self._needs_a_page()
        made = self.browser.film(frames=frames, shots=shots)
        pngs = made.pop("pngs")
        made["shots"] = [self.save(png) for png in pngs]
        steps = []
        for before, after in zip(pngs, pngs[1:]):
            diff = imagediff.compare(before, after)
            steps.append(round(diff.fraction, 4))
        made["motion"] = steps               # fraction changed, frame to frame
        made["moving"] = any(f > 0 for f in steps)
        made["pngs"] = pngs
        return made

    def capture(self, whole_page: bool = False, max_edge: int = MAX_SHOT_EDGE) -> tuple[bytes, dict]:
        """A PNG plus what it is a picture of."""
        self._needs_a_page()
        probe = self.browser.probe()
        clip = None if whole_page else self.browser.canvas_clip(probe)
        png = self.browser.screenshot(clip, max_edge=max_edge)
        meta = {"clipped": bool(clip), "page": self.page, "url": self.browser.url,
                "bytes": len(png)}
        if clip:
            meta["region"] = {k: round(float(clip[k]), 1) for k in ("x", "y", "width", "height")}
        return png, meta

    # ---------------------------------------------------------------- shots

    def save(self, png: bytes) -> str:
        """Write a screenshot where the UI can fetch it. Returns its relative path.

        On disk rather than in the event: a session's events are already read
        back whole by the history panel, and a base64 PNG in every one of them
        is how that panel went from fast to unusable once before.
        """
        step = _SAFE.sub("-", self.step_id or "step") or "step"
        folder = self.project / SHOTS_DIR / step
        folder.mkdir(parents=True, exist_ok=True)
        # Carry on from whatever is already there. The counter used to start at
        # zero for every agent turn while the folder stayed per step, so the
        # second block of a step wrote over the first one's pictures — and a
        # line saying "nothing changed" then sat above two visibly different
        # images, because the pair it had compared was gone. Measured on one
        # step: five of the last six "byte-for-byte identical" claims pointed
        # at files that differ.
        if self._shots == 0:
            self._shots = max((int(found.stem) for found in folder.glob("*.png")
                               if found.stem.isdigit()), default=0)
        self._shots += 1
        name = f"{self._shots:03d}.png"
        (folder / name).write_bytes(png)
        return f"{step}/{name}"
