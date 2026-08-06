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

#: Past either of these the memory is compacted. They are deliberately below
#: MAX_PROMPT_CHARS: once notes are being dropped from the prompt, agents are
#: already working from a partial picture, and compaction should have happened
#: before that — not as a consequence of it.
MAX_NOTES = 25
MAX_CHARS = 3000

COMPACT_PROMPT = """\
Below is a team of coding agents' shared memory for one project. Every line is \
read into every agent's prompt, so it has to stay short.

Rewrite it as a shorter list of the facts that are still true and still matter:

- Merge notes that say the same thing.
- Where a later note supersedes an earlier one (a port changed, a route was \
renamed), keep ONLY the current fact.
- Drop anything that was about doing the work rather than about the result, and \
anything obvious from reading the code.
- Keep contracts between components, ports and paths, formats, and commands. \
When in doubt, keep it.

Do not invent, generalise, or soften. Reply with the list only — one fact per \
line, each starting with "- " and keeping its **author** tag. No preamble."""

_HEADER = """# Project memory

Shared by every agent on this project. Facts and decisions that outlive the step
that made them: contracts between components, conventions, how to run things.
Edit or delete freely — this file is read into every agent's prompt.
"""


#: Notes written by hand, through the UI or the file. `remember` always stamps
#: the agent's name, so this can only be the user.
_USER_AUTHORS = ("user", "you", "human", "me")


def _is_users(note: str) -> bool:
    match = re.match(r"^-\s*\*\*(?P<who>[^*]+)\*\*\s*:", note)
    return bool(match) and match.group("who").strip().lower() in _USER_AUTHORS


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

    # ----------------------------------------------------------- compaction

    def oversized(self, max_notes: int = MAX_NOTES, max_chars: int = MAX_CHARS) -> bool:
        notes = self.notes()
        return len(notes) > max_notes or sum(len(n) for n in notes) > max_chars

    def compact(self, rewrite, *, max_notes: int = MAX_NOTES) -> dict:
        """Rewrite the notes into fewer, using `rewrite(text) -> text`.

        Trimming the oldest would be simpler and wrong: the first thing written
        is usually the API contract, and the fortieth is usually a detail. What
        has to go is what is *superseded or duplicated*, and only something that
        can read the notes knows which those are.

        The previous file is archived first. Compaction that silently discards
        is worse than a long memory — you cannot audit what an agent was told.
        """
        before = self.notes()
        if not before:
            return {"compacted": False, "reason": "nothing to compact"}

        # A note the user wrote is an instruction, not an observation. Handing it
        # to a model to "merge and shorten" is how a deliberate correction gets
        # summarised away by the very agents it was meant to correct.
        mine = [n for n in before if _is_users(n)]
        theirs = [n for n in before if not _is_users(n)]
        if not theirs:
            return {"compacted": False, "reason": "only user notes, which are kept verbatim"}

        try:
            proposed = rewrite("\n".join(theirs))
        except Exception as exc:                       # a failed rewrite is not fatal
            return {"compacted": False, "reason": f"rewrite failed: {exc}"}

        kept = [line.strip() for line in (proposed or "").splitlines()
                if line.strip().startswith("- ")]
        # Guards against a model that answers with prose, an apology, or nothing.
        # Losing the team's shared facts is far worse than a memory that is long.
        if not kept:
            return {"compacted": False, "reason": "the rewrite produced no notes"}
        if len(kept) > len(theirs):
            return {"compacted": False, "reason": "the rewrite was not shorter"}
        kept = mine + kept          # the user's notes survive intact, and lead

        with self._lock:
            archive = self.path.with_name("memory.archive.md")
            with archive.open("a", encoding="utf8") as handle:
                handle.write(f"\n\n## Compacted from {len(before)} to {len(kept)} notes\n"
                             + "\n".join(before) + "\n")
            self.path.write_text(_HEADER + "\n" + "\n".join(kept) + "\n", encoding="utf8")
        return {"compacted": True, "before": len(before), "after": len(kept),
                "archive": str(archive), "notes": kept}

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


def write_plan(project, goal: str, steps: list) -> Path | None:
    """Write the plan to `.trance/PLAN.md`, for the person watching.

    Not README.md: that is the project's own documentation and belongs to
    whoever ends up reading the repo, not to the machinery that built it.
    Not the agents' prompts either — they are given the goal and the two steps
    after theirs, which is orientation; the full list is an invitation to do
    someone else's step. This file exists so a human can see, at a glance and
    after the fact, what the run was trying to do.
    """
    path = Path(project) / ".trance" / "PLAN.md"
    lines = ["# Plan", ""]
    if goal:
        lines += ["## Goal", "", goal, ""]
    lines += ["## Steps", ""]
    for i, step in enumerate(steps, 1):
        mark = {"done": "x", "failed": "!", "skipped": "-"}.get(step.status, " ")
        bits = [f"{step.role}"]
        if step.points:
            bits.append(f"{step.points} pts")
        if step.checker:
            bits.append(f"checked by {step.checker}")
        lines.append(f"{i}. [{mark}] **{' · '.join(bits)}** — {step.task}")
    lines.append("")
    lines.append("_Written by trance. Edits here do not change the flow; edit that in the UI._")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf8")
    except OSError:
        return None
    return path
