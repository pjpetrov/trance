"""The team's shared notebook: `.trance/memory.md` in the project.

Every agent starts a step with an empty conversation. That is deliberate — it
is what keeps context small — but it means decisions evaporate. The backend
picks port 3100 and a route shape; the frontend, a step later, has no way to
know either, so it guesses, and the tester then finds a "bug" that is really
two agents disagreeing.

The step history already carries *what happened* (one line per step, written by
the engine). This is the other half: what was **decided**, written deliberately
by the agent that decided it, in the words the next agent needs. It is a small
file on purpose — a note that has to be read by every agent for the rest of the
run earns its place or it does not belong here.

It lives in the project rather than the run store so the user can read and edit
it, and so it survives across sessions on the same codebase.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

MEMORY_FILE = "memory.md"

#: How much of the file is injected into an agent's prompt. Past this the notes
#: are no longer "the few things everyone must know".
MAX_PROMPT_CHARS = 4000
#: One note. Long enough for a route signature, short enough to stay a note.
MAX_NOTE_CHARS = 400

_HEADER = """# Project memory

Shared by every agent on this project. Facts and decisions that outlive the step
that made them: contracts between components, conventions, how to run things.
Edit or delete freely — this file is read into every agent's prompt.
"""


def _normalize(text: str) -> str:
    """Compare notes by what they say, not how they were punctuated.

    Two agents restating the same decision is the common case — a trailing full
    stop or a capital letter must not buy the note a second slot in everyone's
    prompt.
    """
    collapsed = re.sub(r"\s+", " ", (text or "").strip())
    return collapsed.strip(" .;,:!*-#").lower()


class ProjectMemory:
    """Append-mostly notes, deduplicated, bounded, readable by hand."""

    def __init__(self, project: Path):
        self.path = Path(project) / ".trance" / MEMORY_FILE
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ io

    def raw(self) -> str:
        try:
            return self.path.read_text(encoding="utf8")
        except OSError:
            return ""

    def notes(self) -> list[str]:
        return [line.strip() for line in self.raw().splitlines()
                if line.strip().startswith("- ")]

    def note(self, agent: str, text: str) -> tuple[bool, str]:
        """Record one note. Returns (stored, message_for_the_agent)."""
        text = " ".join((text or "").split())
        if not text:
            return False, "Nothing to remember: the note was empty."
        if len(text) > MAX_NOTE_CHARS:
            text = text[:MAX_NOTE_CHARS].rstrip() + "…"

        with self._lock:
            existing = self.notes()
            key = _normalize(text)
            for line in existing:
                # Same fact twice is noise every agent then pays for.
                if _normalize(line.split(":", 1)[-1]) == key:
                    return False, f"Already in project memory, unchanged: {text}"

            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                self.path.write_text(_HEADER, encoding="utf8")
            with self.path.open("a", encoding="utf8") as handle:
                handle.write(f"\n- **{agent}**: {text}")
        return True, f"Remembered, and every agent after you will see it: {text}"

    # -------------------------------------------------------------- prompt

    def for_prompt(self, budget: int = MAX_PROMPT_CHARS) -> str:
        """The notes as they go into an agent's prompt, newest kept."""
        notes = self.notes()
        if not notes:
            return ""
        kept: list[str] = []
        used = 0
        for line in reversed(notes):            # newest first while budgeting
            if used + len(line) > budget:
                break
            kept.append(line)
            used += len(line) + 1
        dropped = len(notes) - len(kept)
        body = "\n".join(reversed(kept))        # ...but chronological on screen
        if dropped:
            body = f"({dropped} older note(s) omitted)\n{body}"
        return body
