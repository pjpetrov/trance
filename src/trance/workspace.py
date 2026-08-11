"""What a project configures for itself, kept in the project.

Agents, loops, command allowlists and the run settings used to live in one
workspace-wide `runs/` directory, shared by every session. That made two things
impossible: tuning an agent for one project without changing it for all of them,
and handing a project to someone else complete. They now live in the project's
own `.trance/`, so copying that folder copies the way the project is built.

Models stay in `runs/providers.json`, deliberately. They carry API keys, and a
folder you copy, zip and share is the last place a key should be. A project
names a model; the trance it lands in resolves that name against its own.

A project with no `.trance/` yet is seeded from the workspace-wide files — the
setup you already have, rather than the shipped defaults. That one rule covers
both cases people care about: a new project starts from what you use, and a
project made before any of this existed picks it up the first time it is opened.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .agents.store import CommandStore, LoopStore, RoleStore

#: Where a project keeps what trance knows about it. Already holds the graph
#: index, the plan, the shared memory and the screenshots.
STORE_DIR = ".trance"

AGENTS = "agents.json"
LOOPS = "loops.json"
COMMANDS = "commands.json"
SETTINGS = "settings.json"


@dataclass
class Settings:
    """Run settings that belong to a project rather than to the machine.

    These were never written down anywhere before: they lived in memory and
    reset on every restart, so turning off commits lasted until the next one.
    """

    max_step_points: int = 5
    escalation_preset: str = ""
    escalation_role: str = ""
    git_commits: bool = True
    git_auto_init: bool = True
    #: An agent whose step opens every generated plan — a planner going over
    #: the request before anyone builds. Empty = plans open with whatever the
    #: orchestrator proposed.
    plan_open: str = ""
    #: An agent or loop appended to every generated plan that does not already
    #: end with it — "always finish by looking at the running app" as a rule
    #: rather than a hope. Empty = the existing final-check guarantee alone.
    plan_close: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


class SettingsStore:
    """The project's run settings, as JSON beside its other state."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.settings = Settings()
        if self.path.is_file():
            try:
                self.settings = Settings.from_dict(
                    json.loads(self.path.read_text(encoding="utf8")))
            except (OSError, ValueError):
                pass                      # a corrupt file is not worth a crash

    def update(self, **changes) -> Settings:
        known = {f.name for f in fields(Settings)}
        for key, value in changes.items():
            if key in known and value is not None:
                setattr(self.settings, key, value)
        self._save()
        return self.settings

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self.settings.to_dict(), indent=2),
                                 encoding="utf8")
        except OSError:
            pass


#: Everything a folder name may keep; the rest becomes a dash.
_KEEP = re.compile(r"[^a-z0-9._-]+")


def folder_for(name: str) -> str:
    """The folder a project called `name` gets inside the workspace.

    Typing an absolute path was the one piece of ceremony between "I want to
    build this" and building it, and it was the same path every time with a
    different last component. The name is that component.

    Deliberately the same folder for the same name: a second session on a
    project you already have should join it, not start an empty copy beside it
    — which is what the stores already assume, holding a project by its path.

    Separators do not survive, so a name cannot reach outside the workspace.
    """
    slug = _KEEP.sub("-", name.strip().lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-._")[:64].strip("-._")
    return slug or "project"


def seed(target: Path, source: Path | None) -> bool:
    """Copy a workspace-wide file into a project that has none. True if copied.

    Never overwrites: once a project has its own, that is the one that counts,
    and a later change to the workspace-wide file must not reach back into
    projects that have moved on.
    """
    if target.exists() or source is None or not Path(source).is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return True


class ProjectStores:
    """One project's agents, loops, allowlists and settings."""

    def __init__(self, project: Path, defaults: Path | None = None):
        self.project = Path(project)
        self.dir = self.project / STORE_DIR
        self.dir.mkdir(parents=True, exist_ok=True)

        # Seeded before each store opens, so a store that fills in defaults for
        # a missing file never gets the chance to — the copy is already there.
        self.seeded = {
            name: seed(self.dir / name, (defaults / name) if defaults else None)
            for name in (AGENTS, LOOPS, COMMANDS, SETTINGS)
        }

        self.roles = RoleStore(self.dir / AGENTS)
        self.loops = LoopStore(self.dir / LOOPS)
        self.commands = CommandStore(self.dir / COMMANDS)
        self.settings = SettingsStore(self.dir / SETTINGS)

    @property
    def migrated(self) -> bool:
        """Whether anything was carried in from the workspace-wide files."""
        return any(self.seeded.values())


class DefaultStores:
    """The workspace-wide configuration every new project is seeded from.

    Same shape as ProjectStores, so nothing downstream has to know which of the
    two it is holding. No `.trance/` and no seeding: these files *are* the
    source, sitting where the installation keeps its state.

    Editable for the same reason the per-project copies are. Tuning an agent
    used to mean tuning it for every project at once; per-project stores fixed
    that and left no way to change what the *next* project starts from — so a
    prompt you had improved in four projects still had to be improved a fifth
    time in the fifth.
    """

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.project = self.dir
        self.seeded: dict[str, bool] = {}

        self.roles = RoleStore(self.dir / AGENTS)
        self.loops = LoopStore(self.dir / LOOPS)
        self.commands = CommandStore(self.dir / COMMANDS)
        self.settings = SettingsStore(self.dir / SETTINGS)

    @property
    def migrated(self) -> bool:
        return False


class Workspace:
    """Every project's stores, opened once each.

    Held by path rather than by session id: two sessions on the same directory
    are two views of one project, and giving them separate stores would let them
    disagree about what the agents are.
    """

    def __init__(self, defaults: Path | None = None):
        self.defaults = Path(defaults) if defaults else None
        self._open: dict[Path, ProjectStores] = {}

    def stores_for(self, project: Path | str) -> ProjectStores:
        key = Path(project).expanduser().resolve()
        held = self._open.get(key)
        if held is None:
            held = ProjectStores(key, self.defaults)
            self._open[key] = held
        return held

    def forget(self, project: Path | str) -> None:
        """Drop a project's stores — after its directory has been deleted."""
        self._open.pop(Path(project).expanduser().resolve(), None)
