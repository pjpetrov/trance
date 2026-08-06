"""The agent library: editable agent types, persisted to JSON.

`BUILTIN_ROLES` seeds the library once; after that `runs/agents.json` is the
source of truth, so edits made in the UI survive a restart and apply everywhere
that agent type is used.

An agent type owns four things the user cares about:
  * its **remit** (`paths`) — what it may write, enforced at the tool boundary
  * its **toolsets** — files / graph / commands, i.e. what it can do at all
  * its **model** (`preset`) — which named model runs it
  * its **prompt** — how it behaves and, for verifiers, what "verified" means
"""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

from .roles import BUILTIN_ROLES, TOOLSETS, AgentRole
from .tools import ALLOWED_COMMANDS, CommandPolicy

#: Types that ship with trance. They can be edited, but not deleted — deleting
#: one would break flows that name it, and re-adding it by hand is fiddly.
PROTECTED = frozenset(BUILTIN_ROLES)


class RoleStore:
    def __init__(self, path: Path, seed: dict[str, AgentRole] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._roles: dict[str, AgentRole] = {}

        if self.path.exists():
            self._load()
        # Any built-in missing from disk is restored, so upgrading trance adds
        # new agent types without wiping the user's edits to existing ones.
        # deepcopy is load-bearing: handing out the shared BUILTIN_ROLES object
        # means an edit mutates the shipped default in place, and `reset()`
        # would then restore the very edit it is meant to undo.
        for name, role in (seed or BUILTIN_ROLES).items():
            self._roles.setdefault(name, copy.deepcopy(role))
        self._save()

    # ----------------------------------------------------------------- io

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf8"))
        except (OSError, json.JSONDecodeError):
            return
        for item in data.get("agents", []):
            try:
                role = AgentRole.from_dict(item)
            except TypeError:
                continue
            if role.name:
                self._roles[role.name] = role

    def _save(self) -> None:
        payload = {"agents": [r.to_dict() for r in self._roles.values()]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf8")
        tmp.replace(self.path)

    # ---------------------------------------------------------------- api

    def all(self) -> list[AgentRole]:
        return sorted(self._roles.values(), key=lambda r: r.name)

    def get(self, name: str | None) -> AgentRole | None:
        return self._roles.get(name) if name else None

    def upsert(self, role: AgentRole) -> AgentRole:
        with self._lock:
            self._roles[role.name] = role
            self._save()
        return role

    def delete(self, name: str) -> bool:
        if name in PROTECTED:
            return False
        with self._lock:
            removed = self._roles.pop(name, None) is not None
            if removed:
                self._save()
        return removed

    def reset(self, name: str) -> AgentRole | None:
        """Restore a built-in agent to its shipped definition.

        Stored edits are never overwritten on load, which is right — but it also
        means prompt improvements that ship with a new trance version don't
        reach an agent you've already saved. This is the opt-in.
        """
        shipped = BUILTIN_ROLES.get(name)
        if shipped is None:
            return None
        role = copy.deepcopy(shipped)
        with self._lock:
            self._roles[name] = role
            self._save()
        return role

    def resolve_team(self, names_or_roles) -> list[AgentRole]:
        """Refresh a session's team from the library.

        A session stores *which* agent types are on its team; the library owns
        what each one is. Without this, editing an agent type would leave every
        existing session running the copy it was created with.
        """
        out: list[AgentRole] = []
        for item in names_or_roles:
            name = item if isinstance(item, str) else getattr(item, "name", None)
            role = self.get(name)
            if role is not None and role not in out:
                out.append(role)
        return out


def validate(data: dict) -> str | None:
    """Return an error message, or None if the agent definition is usable."""
    name = (data.get("name") or "").strip()
    if not name or " " in name:
        return "name must be non-empty and contain no spaces"
    bad = [t for t in (data.get("toolsets") or []) if t not in TOOLSETS]
    if bad:
        return f"unknown toolset(s): {', '.join(bad)}. Allowed: {', '.join(TOOLSETS)}"
    if data.get("paths") and not isinstance(data["paths"], list):
        return "paths must be a list of globs"
    toolsets = data.get("toolsets") or []
    if "files" in toolsets and not data.get("paths"):
        return ("an agent with the files toolset needs at least one path in its remit, "
                "or every write it attempts will be refused")
    return None


#: The list every agent uses unless it names another. Kept as a real entry so
#: "default" is editable like any other rather than being a special case.
DEFAULT_LIST = "default"


class CommandStore:
    """Named command allowlists, persisted so UI edits survive a restart.

    One global list was the wrong shape: a tester needs npx and jest, devops
    needs npm and docker, and a reviewer needs neither. Naming the lists lets an
    agent point at the one that fits instead of everyone sharing the union of
    everything anyone ever needed.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.lists: dict[str, CommandPolicy] = {}
        if self.path.exists():
            self._load()
        self.lists.setdefault(DEFAULT_LIST, CommandPolicy())
        self._save()

    # ------------------------------------------------------------------ io

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf8"))
        except (OSError, json.JSONDecodeError):
            return
        named = data.get("lists")
        if isinstance(named, dict):
            for name, raw in named.items():
                self.lists[name] = _policy_from(raw)
            return
        # Older single-list file: keep what the user had, under "default".
        if data.get("allowed"):
            self.lists[DEFAULT_LIST] = _policy_from(data)

    def _save(self) -> None:
        payload = {"lists": {n: p.to_dict() for n, p in self.lists.items()}}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf8")
        tmp.replace(self.path)

    # ----------------------------------------------------------------- api

    @property
    def policy(self) -> CommandPolicy:
        """The default list — what an agent gets when it names none."""
        return self.lists[DEFAULT_LIST]

    def get(self, name: str | None) -> CommandPolicy:
        return self.lists.get(name or DEFAULT_LIST) or self.policy

    def names(self) -> list[str]:
        return sorted(self.lists, key=lambda n: (n != DEFAULT_LIST, n))

    def upsert(self, name: str, allowed=None, shell=None) -> CommandPolicy:
        with self._lock:
            policy = self.lists.setdefault(name, CommandPolicy(allowed=[], shell=True))
            if allowed is not None:
                policy.allowed = sorted({str(c).strip() for c in allowed if str(c).strip()})
            if shell is not None:
                policy.shell = bool(shell)
            self._save()
        return policy

    def delete(self, name: str) -> bool:
        """Remove a list. The default cannot go — something has to be the floor."""
        if name == DEFAULT_LIST:
            return False
        with self._lock:
            removed = self.lists.pop(name, None) is not None
            if removed:
                self._save()
        return removed

    def update(self, allowed=None, shell=None) -> CommandPolicy:
        """Edit the default list. Kept for the single-list API and the CLI."""
        return self.upsert(DEFAULT_LIST, allowed=allowed, shell=shell)

    def reset(self, name: str = DEFAULT_LIST) -> CommandPolicy:
        return self.upsert(name, allowed=sorted(ALLOWED_COMMANDS), shell=True)


def _policy_from(raw: dict) -> CommandPolicy:
    allowed = sorted({str(c).strip() for c in (raw.get("allowed") or []) if str(c).strip()})
    return CommandPolicy(allowed=allowed or sorted(ALLOWED_COMMANDS),
                         shell=bool(raw.get("shell", True)))
