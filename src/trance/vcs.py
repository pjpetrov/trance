"""Git checkpoints around each step.

An agent that goes wrong leaves the project worse than it found it, and the only
honest way to undo that is the one every developer already trusts. So each step
runs between two commits: one taken before it starts, one after it finishes.
The first is what "revert this step" means; the second is what makes the run
readable afterwards — `git log` becomes the list of what each agent did.

Everything here is deliberately narrow:

* It only ever touches the working tree and the current branch. No pushing, no
  rebasing, no branch switching, no history rewriting.
* A revert is opt-in per step, because throwing away work is not something to
  do on a guess.
* Nothing raises. Git being absent, the project not being a repository, a
  commit failing — none of that is a reason to stop a run, so every call
  returns a result object that says what happened.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

#: Long enough for a slow first `git add` on a large tree, short enough that a
#: hung git does not hold a run.
TIMEOUT_S = 60

#: Prefix on every message trance writes, so its commits are greppable and
#: obviously not the user's.
PREFIX = "trance"


@dataclass
class GitResult:
    ok: bool
    detail: str = ""
    sha: str = ""
    files: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


def _run(project: Path, *args: str) -> tuple[int, str]:
    try:
        done = subprocess.run(
            ["git", *args], cwd=str(project), capture_output=True, text=True,
            timeout=TIMEOUT_S,
        )
    except FileNotFoundError:
        return 127, "git is not installed"
    except (subprocess.SubprocessError, OSError) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout or done.stderr or "").strip()


def available() -> bool:
    code, _ = _run(Path.cwd(), "--version")
    return code == 0


def is_repo(project: Path) -> bool:
    code, out = _run(project, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out.strip() == "true"


def ensure_repo(project: Path) -> GitResult:
    """Make the project a repository if it is not one already.

    A checkpoint needs somewhere to live, and asking the user to run `git init`
    before trance is useful is a worse trade than creating an empty repository
    they can delete.
    """
    if is_repo(project):
        return GitResult(True, "already a repository")
    code, out = _run(project, "init")
    if code != 0:
        return GitResult(False, out)
    # A repository with no identity cannot commit, and the global config may
    # not have one. Set it locally so nothing outside this project changes.
    _run(project, "config", "user.email", "agents@trance.local")
    _run(project, "config", "user.name", "trance agents")
    ignore_trance_files(project)
    return GitResult(True, "initialised a git repository")


#: trance's own working files, which live in the project but are not part of it.
#: The graph database in particular is binary and rewritten on every index, so
#: committing it puts "Binary files differ" in the middle of every diff an agent
#: made — in a repo whose whole point is being readable afterwards.
IGNORED = (".trance/graph.db", ".trance/graph.db-shm", ".trance/graph.db-wal")
_IGNORE_BLOCK = "\n".join(("# trance's index — regenerated, not source", *IGNORED))


def ignore_trance_files(project: Path) -> bool:
    """Add trance's own working files to .gitignore. True if it changed it.

    PLAN.md and memory.md are deliberately *not* ignored: they are written for
    you to read, and their history is part of the record of the run.
    """
    path = Path(project) / ".gitignore"
    try:
        current = path.read_text(encoding="utf8") if path.exists() else ""
        if ".trance/graph.db" in current:
            return False
        prefix = "" if not current or current.endswith("\n") else "\n"
        path.write_text(current + prefix + _IGNORE_BLOCK + "\n", encoding="utf8")
    except OSError:
        return False
    return True


def untrack_ignored(project: Path) -> list[str]:
    """Stop tracking trance's index in a repo that already committed it.

    Only ever from the index — the files stay on disk, and no history is
    rewritten. Returns what it stopped tracking.
    """
    code, out = _run(project, "ls-files", "--", ".trance/")
    if code != 0 or not out:
        return []
    tracked = [line.strip() for line in out.splitlines()
               if line.strip().split("/")[-1].startswith("graph.db")]
    if not tracked:
        return []
    _run(project, "rm", "--cached", "-q", "--", *tracked)
    return tracked


def head(project: Path) -> str:
    code, out = _run(project, "rev-parse", "HEAD")
    return out.strip() if code == 0 else ""


def dirty(project: Path) -> list[str]:
    """Paths that differ from HEAD, including untracked ones."""
    code, out = _run(project, "status", "--porcelain")
    if code != 0 or not out:
        return []
    # "XY PATH", and the two status columns can each be blank or set.
    return [line[2:].strip() for line in out.splitlines() if line.strip()]


def commit_all(project: Path, message: str) -> GitResult:
    """Commit everything in the tree. Returns ok=False when there is nothing.

    `git add -A` then commit: an agent's work is whatever it left on disk, and
    working out which paths it touched is the tool layer's job, not this one's.
    """
    changed = dirty(project)
    if not changed:
        return GitResult(False, "nothing to commit")

    code, out = _run(project, "add", "-A")
    if code != 0:
        return GitResult(False, f"git add failed: {out}")
    code, out = _run(project, "commit", "-m", f"{PREFIX}: {message}", "--no-verify")
    if code != 0:
        return GitResult(False, f"git commit failed: {out}")
    return GitResult(True, out.splitlines()[0] if out else "committed",
                     sha=head(project), files=changed)


def undo(project: Path, commit_sha: str, checkpoint: str = "") -> GitResult:
    """Undo a step's work, leaving both the work and the undo in history.

    A hard reset would be simpler and would throw the commit away with it —
    "reverted" would then mean the agent's work is gone, and reading back what
    it tried would need the reflog. `git revert` adds an inverse commit instead:
    the tree goes back to where the step started and `git log` still shows both
    what was done and that it was undone.

    Only a step's own commit is ever reverted, which is the tip, so it always
    applies cleanly. When the step committed nothing there is dirt to discard
    instead, and that is what `checkpoint` is for.
    """
    if commit_sha:
        code, out = _run(project, "revert", "--no-edit", commit_sha)
        if code != 0:
            _run(project, "revert", "--abort")
            return GitResult(False, f"git revert failed: {out}")
        return GitResult(True, f"reverted {commit_sha[:8]}", sha=head(project))

    # Nothing was committed, so whatever is here is the step's and unwanted.
    lost = dirty(project)
    if not lost:
        return GitResult(True, "nothing to undo", sha=checkpoint)
    if checkpoint:
        code, out = _run(project, "reset", "--hard", checkpoint)
        if code != 0:
            return GitResult(False, f"git reset failed: {out}")
    code, clean_out = _run(project, "clean", "-fd")
    removed = [line.replace("Removing ", "").strip()
               for line in clean_out.splitlines() if line.startswith("Removing ")]
    return GitResult(True, "discarded uncommitted changes", sha=checkpoint,
                     files=lost + removed)


def log(project: Path, limit: int = 20) -> list[dict]:
    """Recent commits, for showing what a run actually produced."""
    code, out = _run(project, "log", f"-{limit}", "--pretty=format:%H%x1f%s%x1f%ar")
    if code != 0 or not out:
        return []
    entries = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3:
            entries.append({"sha": parts[0], "subject": parts[1], "when": parts[2]})
    return entries


def commits_between(project: Path, before: str, after: str = "HEAD") -> list[dict]:
    """The commits that made up a piece of work, oldest first.

    One commit per step is what the engine writes, so this is the list of what
    each agent actually did — which is a more useful answer to "what was fixed"
    than one combined diff, and the only way to see the order it happened in.
    """
    if not before:
        return []
    code, out = _run(project, "log", "--reverse", "--no-merges",
                     f"{before}..{after}", "--pretty=format:%H%x1f%s%x1f%ar%x1f%an",
                     "--shortstat")
    if code != 0 or not out:
        return []

    commits: list[dict] = []
    for line in out.splitlines():
        if "\x1f" in line:
            sha, subject, when, who = (line.split("\x1f") + ["", "", ""])[:4]
            commits.append({"sha": sha, "short": sha[:8], "subject": subject,
                            "when": when, "who": who, "files": 0,
                            "added": 0, "removed": 0})
        elif line.strip() and commits:
            # " 2 files changed, 30 insertions(+), 4 deletions(-)"
            for count, what in re.findall(r"(\d+) (\w+)", line):
                if what.startswith("file"):
                    commits[-1]["files"] = int(count)
                elif what.startswith("insertion"):
                    commits[-1]["added"] = int(count)
                elif what.startswith("deletion"):
                    commits[-1]["removed"] = int(count)
    return commits


def show(project: Path, sha: str, max_chars: int = 400_000) -> dict:
    """One commit: what it says, and what it changed."""
    if not sha or not re.fullmatch(r"[0-9a-fA-F]{4,40}", sha):
        return {}
    code, out = _run(project, "show", sha, "--stat", "--pretty=format:%H%x1f%s%x1f%ar%x1f%an")
    if code != 0 or not out:
        return {}
    head_line, _, rest = out.partition("\n")
    sha_full, subject, when, who = (head_line.split("\x1f") + ["", "", ""])[:4]

    code, patch = _run(project, "show", sha, "--patch", "--pretty=format:")
    patch = patch if code == 0 else ""
    clipped = len(patch) > max_chars
    return {"sha": sha_full, "short": sha_full[:8], "subject": subject, "when": when,
            "who": who, "stat": rest.strip(), "diff": patch[:max_chars],
            "clipped": clipped}


def diff(project: Path, before: str, after: str = "HEAD", path: str = "") -> str:
    """What changed between two commits, optionally for one file."""
    if not before:
        return ""
    args = ["diff", f"{before}..{after}"]
    if path:
        args += ["--", path]
    code, out = _run(project, *args)
    return out if code == 0 else ""


def changed_between(project: Path, before: str, after: str = "HEAD") -> list[str]:
    """The files that differ between two commits."""
    if not before:
        return []
    code, out = _run(project, "diff", "--name-only", f"{before}..{after}")
    return [line.strip() for line in out.splitlines() if line.strip()] if code == 0 else []
