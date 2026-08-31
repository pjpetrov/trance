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

from ..loops import Loop
from .roles import _DEFAULTS, BUILTIN_ROLES, TOOLSETS, AgentRole, definition_differs
from .tools import ALLOWED_COMMANDS, CommandPolicy

#: Types that ship with trance. Once undeletable, on the theory that a flow
#: naming one would break — but every agent is deletable now, by the user's
#: rule: "all should be deletable". A deleted built-in leaves a tombstone so
#: the upgrade path (restore any built-in missing from disk) does not bring
#: it back; a step that still names it fails saying it was deleted, which is
#: what deleting a custom agent always did. Kept as the set of names the UI
#: marks "built-in", for the reset button.
PROTECTED = frozenset(BUILTIN_ROLES)


#: Settings a stored agent inherits from its built-in when the file predates
#: them. The value that means "not chosen", per field.
#:
#: `enabled` is here so that shipping a built-in switched off reaches stores
#: written before the switch existed: a file with no `enabled` key never made
#: that choice, so it takes trance's. Once the user touches the switch the key
#: is on disk and their choice stands, on or off.
INHERITED_WHEN_UNSET = {"tool_rounds": 0, "enabled": True}


class RoleStore:
    def __init__(self, path: Path, seed: dict[str, AgentRole] | None = None,
                 overlay: bool = False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._roles: dict[str, AgentRole] = {}
        #: Built-ins the user deleted. Without this the next load would put
        #: them straight back — restoring a missing built-in is how upgrades
        #: deliver new agents, and a deletion has to be told apart from that.
        self._deleted: set[str] = set()
        #: The Default scope is an overlay on shipped: a built-in whose
        #: *definition* was never edited keeps tracking trance's shipped
        #: version, so prompt improvements flow into the defaults instead of
        #: freezing at whichever version first wrote the file. Wiring — model,
        #: checks, retries — is always the stored copy's. Sessions stay frozen
        #: copies, deliberately.
        self.overlay = overlay
        #: Per built-in: has its definition been deliberately edited here?
        #: Missing means the file predates the flag — treated as edited, since
        #: an old frozen copy and a real edit are indistinguishable.
        self._definition_edited: dict[str, bool] = {}

        if self.path.exists():
            self._load()
        # Any built-in missing from disk is restored, so upgrading trance adds
        # new agent types without wiping the user's edits to existing ones.
        # deepcopy is load-bearing: handing out the shared BUILTIN_ROLES object
        # means an edit mutates the shipped default in place, and `reset()`
        # would then restore the very edit it is meant to undo.
        for name, role in (seed or BUILTIN_ROLES).items():
            if name not in self._roles and name not in self._deleted:
                self._roles[name] = copy.deepcopy(role)
                # Just seeded from shipped: unedited by construction.
                self._definition_edited[name] = False
        self._save()

    # ----------------------------------------------------------------- io

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf8"))
        except (OSError, json.JSONDecodeError):
            return
        self._deleted = {str(n) for n in data.get("deleted", []) if n}
        for item in data.get("agents", []):
            try:
                role = AgentRole.from_dict(item)
            except TypeError:
                continue
            if not role.name:
                continue
            # A setting the stored copy has never heard of is not a choice to
            # respect — it is a field that did not exist when the file was
            # written. Take the built-in's value, so improvements to the shipped
            # agents reach anyone who never touched that setting, while anything
            # actually chosen is left alone.
            builtin = BUILTIN_ROLES.get(role.name)
            if builtin is not None:
                for field, unset in INHERITED_WHEN_UNSET.items():
                    if field not in item and getattr(builtin, field) != unset:
                        setattr(role, field, getattr(builtin, field))
            if "definition_edited" in item:
                self._definition_edited[role.name] = bool(item["definition_edited"])
            if builtin is None and self._definition_edited.get(role.name) is False:
                # A retired built-in the user never made theirs. Unedited meant
                # the definition was trance's to improve — and retiring it is
                # the last improvement. An edited copy is the user's and stays.
                self._definition_edited.pop(role.name, None)
                continue
            if (self.overlay and builtin is not None
                    and self._definition_edited.get(role.name) is False):
                # Unedited here means: the definition is trance's to improve.
                # Take the current shipped one, keep the stored wiring.
                fresh = copy.deepcopy(builtin)
                for keep in self.RESET_KEEPS:
                    setattr(fresh, keep, copy.deepcopy(getattr(role, keep)))
                role = fresh
            self._roles[role.name] = role

    def _save(self) -> None:
        rows = []
        for role in self._roles.values():
            item = role.to_dict()
            if role.name in self._definition_edited:
                item["definition_edited"] = self._definition_edited[role.name]
            rows.append(item)
        tmp = self.path.with_suffix(".tmp")
        held = {"agents": rows}
        if self._deleted:
            held["deleted"] = sorted(self._deleted)
        tmp.write_text(json.dumps(held, indent=2), encoding="utf8")
        tmp.replace(self.path)

    # ---------------------------------------------------------------- api

    def all(self) -> list[AgentRole]:
        return sorted(self._roles.values(), key=lambda r: r.name)

    def get(self, name: str | None) -> AgentRole | None:
        return self._roles.get(name) if name else None

    def upsert(self, role: AgentRole) -> AgentRole:
        with self._lock:
            self._deleted.discard(role.name)
            builtin = BUILTIN_ROLES.get(role.name)
            if builtin is not None:
                # A wiring-only save keeps tracking shipped; a definition edit
                # is a decision, frozen until reset says otherwise.
                self._definition_edited[role.name] = bool(
                    definition_differs(role, builtin))
            self._roles[role.name] = role
            self._save()
        return role

    def delete(self, name: str) -> bool:
        with self._lock:
            removed = self._roles.pop(name, None) is not None
            if removed:
                if name in PROTECTED:
                    self._deleted.add(name)
                self._definition_edited.pop(name, None)
                self._save()
        return removed

    #: What reset keeps from the stored copy: how *you* wired the agent into
    #: your setup, as opposed to what the agent is. Restoring the prompt used
    #: to take these too, so one click silently unassigned the model and wiped
    #: the checks the user had put on — and the first sign was a verifier that
    #: stopped running.
    RESET_KEEPS = ("preset", "backup_preset", "tries", "backup_tries",
                   "checks", "command_list", "commands", "workdir", "shell",
                   "tool_rounds", "color")

    def reset(self, name: str, source: AgentRole | None = None) -> AgentRole | None:
        """Restore an agent's definition to its original.

        The original is `source` when given — a session resets to the Default
        scope's copy, which is what "the default" means — else the shipped
        built-in. One chain: session → default → shipped, each link restoring
        one hop, never skipping over the user's own defaults.

        Only the *definition* — prompt, remit, toolsets, title. The user's
        wiring — model, checks, retries, allowlist — survives: it was never the
        thing being restored, and it is not the thing that goes stale.
        """
        original = source or BUILTIN_ROLES.get(name)
        if original is None:
            return None
        role = copy.deepcopy(original)
        with self._lock:
            held = self._roles.get(name)
            if held is not None:
                for keep in self.RESET_KEEPS:
                    setattr(role, keep, copy.deepcopy(getattr(held, keep)))
            self._roles[name] = role
            shipped = BUILTIN_ROLES.get(name)
            if shipped is not None:
                self._definition_edited[name] = bool(definition_differs(role, shipped))
            self._save()
        return role

    @property
    def deleted(self) -> frozenset[str]:
        """Built-ins the user deleted here — what a session must not fall back to."""
        return frozenset(self._deleted)

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
    # An empty remit is not a mistake, it is read-only: the files toolset also
    # reads and lists, and reads are never remit-checked. A reviewer that must
    # not touch the code is exactly this, and refusing to save it left no way to
    # express the safest agent there is.
    return None


#: The list every agent uses unless it names another. Kept as a real entry so
#: "default" is editable like any other rather than being a special case.
DEFAULT_LIST = "default"


class CommandStore:
    """Named command allowlists, persisted so UI edits survive a restart.

    One global list was the wrong shape: a tester needs npx and jest, a coder
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


class LoopStore:
    """Reusable loops, persisted to JSON next to the agents.

    Seeded with the shapes people build by hand every time — tester → developer
    → tester, and its visual counterpart — so the feature is legible without
    reading any documentation.
    """

    def __init__(self, path: Path, seed: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._loops: dict[str, Loop] = {}
        if self.path.exists():
            self._load()
        if seed:
            # Any default missing from disk is restored, the same way roles are:
            # this file existing used to mean "seeded already", so a loop added
            # in a later version never reached anyone who had run trance before
            # it shipped. The cost is that deleting a default brings it back on
            # restart — the same trade the agent library already makes, and the
            # one that keeps an upgrade from being invisible.
            added = False
            for loop in default_loops():
                if loop.name not in self._loops:
                    self._loops[loop.name] = loop
                    added = True
            if added:
                self._save()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf8"))
        except (OSError, json.JSONDecodeError):
            return
        for item in data.get("loops", []):
            loop = Loop.from_dict(item)
            if not loop.name:
                continue
            if loop.name in ("test-and-fix", "visual-test-and-fix"):
                # The coders merged into one "developer". These two loops are
                # trance's own — a stored copy still wiring the old names gets
                # the current fixer, where a custom loop is the user's to fix.
                for node in loop.nodes:
                    if node.role in ("backend", "frontend", "fullstack"):
                        node.role = "developer"
            self._loops[loop.name] = loop

    def _save(self) -> None:
        payload = {"loops": [l.to_dict() for l in self._loops.values()]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf8")
        tmp.replace(self.path)

    def all(self) -> list[Loop]:
        return sorted(self._loops.values(), key=lambda l: l.name)

    def get(self, name: str | None) -> Loop | None:
        return self._loops.get(name) if name else None

    def upsert(self, loop: Loop) -> Loop:
        with self._lock:
            self._loops[loop.name] = loop
            self._save()
        return loop

    def reset(self, name: str, source: "Loop | None" = None) -> "Loop | None":
        """Restore a loop to its original — the same chain agents walk.

        `source` is the Default scope's copy when a project resets; else the
        shipped loop of that name. None when there is nothing to restore to —
        a custom loop with no default has no original but its own.
        """
        import copy as _copy

        original = source or next(
            (loop for loop in default_loops() if loop.name == name), None)
        if original is None:
            return None
        with self._lock:
            fresh = _copy.deepcopy(original)
            self._loops[name] = fresh
            self._save()
        return fresh

    def delete(self, name: str) -> bool:
        with self._lock:
            removed = self._loops.pop(name, None) is not None
            if removed:
                self._save()
        return removed


def default_loops() -> list[Loop]:
    """The shipped loops, from trance/defaults/loops.json.

    Data, not code, for the same reason the roster is: the file is the same
    shape the stores speak, so what a fresh install provisions is adjusted by
    editing JSON — or by dropping the files straight into a .trance dir.
    """
    data = json.loads((_DEFAULTS / "loops.json").read_text(encoding="utf8"))
    return [Loop.from_dict(raw) for raw in data["loops"]]
