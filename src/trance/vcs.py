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
IGNORED = (".trance/graph.db", ".trance/graph.db-shm", ".trance/graph.db-wal",
           # Handed to a delegated step's tool server and read back from it.
           # Working files, not the project's.
           ".trance/mcp-calls.jsonl", ".trance/mcp-role.json",
           # Screenshots a visual step took. Evidence for the run, shown in the
           # step history and served from disk — but PNGs, and committing them
           # puts "Binary files differ" through the history of a repo whose
           # whole point is being readable afterwards.
           ".trance/shots/",
           # The project's own agents, loops, allowlists and settings. They are
           # trance's bookkeeping rather than the project's source, and every
           # step commits everything — so tracked, they would appear inside an
           # agent's commit and in the review's list of what it changed. They
           # still travel with the directory, which is how a project is handed
           # over; git is not the channel.
           ".trance/agents.json", ".trance/loops.json",
           ".trance/commands.json", ".trance/settings.json",
           # The session itself — chat, flow, event trace. It lives in the
           # project so the workspace's session list is the workspace's
           # projects, but it is a record *about* the work: committed, every
           # step's commit would carry a rewrite of the session file, and a
           # revert would try to revert the bookkeeping too.
           ".trance/sessions/")
_IGNORE_HEADER = "# trance's index — regenerated, not source"


def ignore_trance_files(project: Path) -> bool:
    """Add trance's own working files to .gitignore. True if it changed it.

    Adds whatever is *missing*, rather than nothing at all once the file has
    been touched before. The all-or-nothing version meant a project set up by an
    older trance never picked up anything added later — and the first thing
    added later was screenshots, which are binaries.

    PLAN.md and memory.md are deliberately *not* ignored: they are written for
    you to read, and their history is part of the record of the run.
    """
    # Also mirrored into .git/info/exclude, which is never tracked: the
    # .gitignore itself rides in commits, so reverting the commit that
    # introduced it deletes it from the tree mid-revert — at which point the
    # very next `git add -A` swept the no-longer-ignored session state into
    # the revert commit. Found live: "apply commits" then refused to touch
    # the tree because the event log it had accidentally tracked kept moving.
    _write_exclude(project)
    path = Path(project) / ".gitignore"
    try:
        current = path.read_text(encoding="utf8") if path.exists() else ""
        listed = {line.strip() for line in current.splitlines()}
        missing = [entry for entry in IGNORED if entry not in listed]
        if not missing:
            return False
        block = "\n".join(([_IGNORE_HEADER] if _IGNORE_HEADER not in current else []) + missing)
        prefix = "" if not current or current.endswith("\n") else "\n"
        path.write_text(current + prefix + block + "\n", encoding="utf8")
    except OSError:
        return False
    return True


def _write_exclude(project: Path) -> None:
    """Keep trance's entries in .git/info/exclude, out of git's own reach."""
    git_dir = Path(project) / ".git"
    if not git_dir.is_dir():
        return                       # a subdir of a larger repo: .gitignore only
    path = git_dir / "info" / "exclude"
    try:
        current = path.read_text(encoding="utf8") if path.exists() else ""
        listed = {line.strip() for line in current.splitlines()}
        missing = [entry for entry in IGNORED if entry not in listed]
        if not missing:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if not current or current.endswith("\n") else "\n"
        path.write_text(current + prefix + "\n".join(missing) + "\n", encoding="utf8")
    except OSError:
        pass


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


def make_branch(project: Path, name: str, sha: str = "HEAD") -> GitResult:
    """A branch at `sha`, so what is about to be left can still be reached."""
    code, out = _run(project, "branch", "-f", name, sha)
    if code != 0:
        return GitResult(False, out.strip() or "git branch failed")
    return GitResult(True, f"branch {name} at {sha[:8]}")


def reset_hard(project: Path, sha: str) -> GitResult:
    """Move the branch and the tree to `sha`, discarding what came after.

    Only ever called with the abandoned tip already saved on a branch — a hard
    reset with no way back is deletion, and deletion is never this layer's
    decision.
    """
    code, out = _run(project, "reset", "--hard", sha)
    if code != 0:
        return GitResult(False, out.strip() or "git reset failed")
    return GitResult(True, f"reset to {sha[:8]}", sha=head(project))


def worktree_add(project: Path, where: Path, sha: str) -> GitResult:
    """A detached checkout of `sha` at `where` — the repo's own copy of an
    older version, readable and runnable while the branch moves on."""
    if (Path(where) / ".git").exists():
        return GitResult(True, "already checked out")
    _run(project, "worktree", "prune")
    code, out = _run(project, "worktree", "add", "--detach", str(where), sha)
    if code != 0:
        return GitResult(False, out.strip() or "git worktree failed")
    return GitResult(True, f"checked out {sha[:8]}")


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


def revert_commits(project: Path, shas: list[str], message: str) -> GitResult:
    """Add one inverse commit undoing `shas`, newest first.

    One commit for the lot rather than one per sha: "revert this step" is one
    act, and reverting it back — changing your mind — should be one act too.
    A conflict aborts the whole thing and leaves the tree exactly as it was:
    a half-applied revert is worse than a failed one, and the caller's promise
    is that failing costs nothing but the click.
    """
    shas = [sha for sha in shas if sha]
    if not shas:
        return GitResult(False, "no commits to revert")
    code, out = _run(project, "revert", "--no-commit", *reversed(shas))
    if code != 0:
        _run(project, "revert", "--abort")
        _run(project, "reset", "--hard", "HEAD")
        return GitResult(False, out.strip() or "git revert failed")
    made = commit_all(project, message)
    if not made:
        _run(project, "reset", "--hard", "HEAD")
        return GitResult(False, f"the revert staged but would not commit: {made.detail}")
    return GitResult(True, f"reverted {len(shas)} commit(s)", sha=head(project))


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
    if not after:
        return []
    # No `before` is not "no answer" — it is the start of history: the first
    # request of a brand-new project proposes before any repo exists, and its
    # iteration was invisible forever because of this guard.
    span = f"{before}..{after}" if before else after
    code, out = _run(project, "log", "--reverse", "--no-merges",
                     span, "--pretty=format:%H%x1f%s%x1f%ar%x1f%an",
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


#: git's well-known hash of the empty tree — the "before" of a repo's first
#: commit, so a range with no left edge can still be diffed.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def changed_between(project: Path, before: str, after: str = "HEAD") -> list[str]:
    """The files that differ between two commits; from birth when `before`
    is empty."""
    if not after:
        return []
    code, out = _run(project, "diff", "--name-only", f"{before or EMPTY_TREE}..{after}")
    return [line.strip() for line in out.splitlines() if line.strip()] if code == 0 else []
