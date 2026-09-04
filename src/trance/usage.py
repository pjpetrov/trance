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
    #: Wall-clock spent inside the calls, for tokens-per-second. Only calls
    #: that reported a duration count toward it, and `timed_output_tokens`
    #: keeps the matching numerator — mixing timed and untimed calls would
    #: make the rate a fiction.
    duration_ms: int = 0
    timed_output_tokens: int = 0
    #: That wall clock, split at the first token: reading the prompt, then
    #: writing the reply. Only streamed calls report it, so these two need not
    #: add up to `duration_ms` — a non-streaming backend contributes to the
    #: total and to neither half.
    prefill_ms: int = 0
    decode_ms: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def tokens_per_second(self) -> float:
        """Generation rate across the timed calls: what the machine actually
        sustains end to end, prompt processing included."""
        if self.duration_ms <= 0:
            return 0.0
        return round(self.timed_output_tokens / (self.duration_ms / 1000), 1)

    @property
    def decode_tokens_per_second(self) -> float:
        """Generation rate with the prompt reading taken out.

        `tokens_per_second` is the end-to-end figure — what the machine
        sustains including however long it spent reading the context. This is
        the other half of the answer: how fast it writes once it starts. On a
        long conversation the two are wildly different numbers, and which one
        is 'the speed of the model' depends entirely on what you are asking.
        """
        if self.decode_ms <= 0:
            return 0.0
        return round(self.timed_output_tokens / (self.decode_ms / 1000), 1)

    def add(self, usage: dict, duration_ms: int = 0,
            prefill_ms: int = 0, decode_ms: int = 0) -> None:
        self.calls += 1
        # Providers disagree on the names; both spellings are common.
        self.input_tokens += int(usage.get("prompt_tokens")
                                 or usage.get("input_tokens") or 0)
        out = int(usage.get("completion_tokens")
                  or usage.get("output_tokens") or 0)
        self.output_tokens += out
        self.cache_read_tokens += int(usage.get("cache_read_tokens")
                                      or usage.get("cache_read_input_tokens") or 0)
        if duration_ms > 0:
            self.duration_ms += int(duration_ms)
            self.timed_output_tokens += out
        self.prefill_ms += int(prefill_ms or 0)
        self.decode_ms += int(decode_ms or 0)

    def to_dict(self) -> dict:
        return {**asdict(self), "total": self.total,
                "tokens_per_second": self.tokens_per_second,
                "decode_tokens_per_second": self.decode_tokens_per_second}


@dataclass
class ToolTime:
    """What the agents' own hands cost: tests, installs, browsers, greps.

    Kept apart from Spend because it belongs to no model. A step that spends
    four minutes in `npm test` spent none of it generating, and counting the
    two together is how "working time" came to look like model time.
    """

    calls: int = 0
    duration_ms: int = 0
    #: The slowest handful, so the answer to "what is actually taking the
    #: time" is a name and not a total.
    by_name: dict = field(default_factory=dict)

    def add(self, name: str, duration_ms: int) -> None:
        self.calls += 1
        self.duration_ms += max(0, int(duration_ms or 0))
        if name:
            row = self.by_name.setdefault(name, {"calls": 0, "duration_ms": 0})
            row["calls"] += 1
            row["duration_ms"] += max(0, int(duration_ms or 0))

    def to_dict(self) -> dict:
        return {"calls": self.calls, "duration_ms": self.duration_ms,
                "by_name": dict(sorted(self.by_name.items(),
                                       key=lambda kv: -kv[1]["duration_ms"]))}


class UsageLedger:
    """Per-session and lifetime token counts, keyed by model definition."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._by_session: dict[str, dict[str, Spend]] = {}
        self._lifetime: dict[str, Spend] = {}
        self._tools_by_session: dict[str, ToolTime] = {}
        self._tools_lifetime = ToolTime()
        self._load()

    # ------------------------------------------------------------ recording

    def record(self, session_id: str, name: str, usage: dict,
               duration_ms: int = 0, prefill_ms: int = 0,
               decode_ms: int = 0) -> None:
        """Count one model call. A call with no usage reported still counts."""
        if not name:
            return
        with self._lock:
            session = self._by_session.setdefault(session_id or "", {})
            session.setdefault(name, Spend()).add(
                usage or {}, duration_ms, prefill_ms, decode_ms)
            self._lifetime.setdefault(name, Spend()).add(
                usage or {}, duration_ms, prefill_ms, decode_ms)
        self._save()

    def record_tool(self, session_id: str, name: str, duration_ms: int) -> None:
        """Count one tool call and what it cost in wall clock."""
        with self._lock:
            self._tools_by_session.setdefault(session_id or "", ToolTime()).add(
                name, duration_ms)
            self._tools_lifetime.add(name, duration_ms)
        self._save()

    def on_event(self, event) -> None:
        """Bus subscriber: every model call and every tool call."""
        kind = getattr(event, "type", "")
        if getattr(event, "replay", False):
            return
        payload = getattr(event, "payload", None) or {}
        if kind == "tool_call":
            # Only the ones that actually ran: a malformed call the tool layer
            # refused never took any time and would only dilute the average.
            spent = int(float(payload.get("duration_ms") or 0))
            if spent > 0:
                self.record_tool(getattr(event, "session_id", ""),
                                 str(payload.get("name") or ""), spent)
            return
        if kind != "model_call":
            return
        # The model *definition* is the useful key — "Sonnet" is what you chose
        # and what you would delete; two definitions can share a model id.
        name = payload.get("preset") or payload.get("model") or ""
        self.record(getattr(event, "session_id", ""), name,
                    payload.get("usage") or {},
                    int(payload.get("duration_ms") or 0),
                    int(float(payload.get("prefill_ms") or 0)),
                    int(float(payload.get("decode_ms") or 0)))

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

    def tools_for_session(self, session_id: str) -> dict:
        with self._lock:
            held = self._tools_by_session.get(session_id or "")
            return held.to_dict() if held else ToolTime().to_dict()

    def tools_lifetime(self) -> dict:
        with self._lock:
            return self._tools_lifetime.to_dict()

    def forget(self, session_id: str) -> None:
        with self._lock:
            self._by_session.pop(session_id, None)
            self._tools_by_session.pop(session_id, None)
        self._save()

    # ---------------------------------------------------------- persistence

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf8"))
        except (OSError, ValueError):
            return
        def spend_of(row: dict) -> Spend:
            return Spend(
                calls=int(row.get("calls") or 0),
                input_tokens=int(row.get("input_tokens") or 0),
                output_tokens=int(row.get("output_tokens") or 0),
                cache_read_tokens=int(row.get("cache_read_tokens") or 0),
                duration_ms=int(row.get("duration_ms") or 0),
                timed_output_tokens=int(row.get("timed_output_tokens") or 0),
                # Absent from every ledger written before the split existed,
                # which reads as "not measured" — the right answer for a call
                # nobody timed that way.
                prefill_ms=int(row.get("prefill_ms") or 0),
                decode_ms=int(row.get("decode_ms") or 0))

        for name, row in (data.get("lifetime") or {}).items():
            self._lifetime[name] = spend_of(row)
        for session, models in (data.get("sessions") or {}).items():
            self._by_session[session] = {
                name: spend_of(row) for name, row in (models or {}).items()}

        def tools_of(row: dict) -> ToolTime:
            return ToolTime(calls=int(row.get("calls") or 0),
                            duration_ms=int(row.get("duration_ms") or 0),
                            by_name=dict(row.get("by_name") or {}))

        self._tools_lifetime = tools_of(data.get("tool_lifetime") or {})
        for session, row in (data.get("tool_sessions") or {}).items():
            self._tools_by_session[session] = tools_of(row or {})

    def _save(self) -> None:
        if not self.path:
            return
        with self._lock:
            data = {
                "lifetime": {n: asdict(s) for n, s in self._lifetime.items()},
                "sessions": {sid: {n: asdict(s) for n, s in models.items()}
                             for sid, models in self._by_session.items()},
                "tool_lifetime": asdict(self._tools_lifetime),
                "tool_sessions": {sid: asdict(t)
                                  for sid, t in self._tools_by_session.items()},
            }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=2), encoding="utf8")
        except OSError:
            pass          # a counter that cannot be written is not worth a crash
