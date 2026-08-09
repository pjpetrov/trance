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
