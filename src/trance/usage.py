"""What each model was actually asked to do, counted.

Two questions, one answer each:

* "What is this run costing me?" — per session, cleared with the session.
* "What have I spent on this model?" — per model definition, kept on disk.

Counted from the event bus rather than at each call site, because every model
call already reports its usage there — the orchestrator's, the workers', the
checks' — and a counter that only some of them remember to update is worse than
none. Tokens, not currency: trance does not know anybody's price list, and a
number invented from a stale table would be believed.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Spend:
    """What one model has been asked to do."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    #: Of the input, what was a cache re-read. Only backends that report it
    #: (Claude Code, the Anthropic API) fill it in. It matters because a cache
    #: read is billed at about a tenth of a fresh token: a delegated Claude
    #: Code step re-reads its whole conversation every internal turn, so its
    #: raw input count runs 20x every other backend while most of it is the
    #: same tokens read back over and over.
    cache_read_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, usage: dict) -> None:
        self.calls += 1
        # Providers disagree on the names; both spellings are common.
        self.input_tokens += int(usage.get("prompt_tokens")
                                 or usage.get("input_tokens") or 0)
        self.output_tokens += int(usage.get("completion_tokens")
                                  or usage.get("output_tokens") or 0)
        self.cache_read_tokens += int(usage.get("cache_read_tokens")
                                      or usage.get("cache_read_input_tokens") or 0)

    def to_dict(self) -> dict:
        return {**asdict(self), "total": self.total}


class UsageLedger:
    """Per-session and lifetime token counts, keyed by model definition."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._by_session: dict[str, dict[str, Spend]] = {}
        self._lifetime: dict[str, Spend] = {}
        self._load()

    # ------------------------------------------------------------ recording

    def record(self, session_id: str, name: str, usage: dict) -> None:
        """Count one model call. A call with no usage reported still counts."""
        if not name:
            return
        with self._lock:
            session = self._by_session.setdefault(session_id or "", {})
            session.setdefault(name, Spend()).add(usage or {})
            self._lifetime.setdefault(name, Spend()).add(usage or {})
        self._save()

    def on_event(self, event) -> None:
        """Bus subscriber: every model call, whoever made it."""
        if getattr(event, "type", "") != "model_call" or getattr(event, "replay", False):
            return
        payload = getattr(event, "payload", None) or {}
        # The model *definition* is the useful key — "Sonnet" is what you chose
        # and what you would delete; two definitions can share a model id.
        name = payload.get("preset") or payload.get("model") or ""
        self.record(getattr(event, "session_id", ""), name, payload.get("usage") or {})

    # ------------------------------------------------------------- reading

    def for_session(self, session_id: str) -> list[dict]:
        """This run's spend, biggest first."""
        with self._lock:
            rows = [{"model": name, **spend.to_dict()}
                    for name, spend in self._by_session.get(session_id, {}).items()]
        return sorted(rows, key=lambda r: -r["total"])

    def lifetime(self) -> dict[str, dict]:
        with self._lock:
            return {name: spend.to_dict() for name, spend in self._lifetime.items()}

    def forget(self, session_id: str) -> None:
        with self._lock:
            self._by_session.pop(session_id, None)
        self._save()

    # ---------------------------------------------------------- persistence

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf8"))
        except (OSError, ValueError):
            return
        for name, row in (data.get("lifetime") or {}).items():
            self._lifetime[name] = Spend(calls=int(row.get("calls") or 0),
                                         input_tokens=int(row.get("input_tokens") or 0),
                                         output_tokens=int(row.get("output_tokens") or 0))
        for session, models in (data.get("sessions") or {}).items():
            self._by_session[session] = {
                name: Spend(calls=int(row.get("calls") or 0),
                            input_tokens=int(row.get("input_tokens") or 0),
                            output_tokens=int(row.get("output_tokens") or 0))
                for name, row in models.items()}

    def _save(self) -> None:
        if not self.path:
            return
        with self._lock:
            data = {
                "lifetime": {n: asdict(s) for n, s in self._lifetime.items()},
                "sessions": {sid: {n: asdict(s) for n, s in models.items()}
                             for sid, models in self._by_session.items()},
            }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=2), encoding="utf8")
        except OSError:
            pass          # a counter that cannot be written is not worth a crash
