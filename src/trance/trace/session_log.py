"""The event trace on disk, one file per session.

The bus keeps history in memory, which is exactly as long as the process lives.
That is fine for watching a run and useless afterwards: restart the server and
every prompt, every command and every loop block a step went through is gone,
leaving a finished step you can no longer explain.

So every event is also appended to `<session>/events.jsonl` as it happens. JSON
Lines because it is append-only and survives a kill mid-write — a truncated last
line costs one event, not the file.

The one thing this cannot do is store everything without bound. A single
model_call carries the whole prompt, so a long run is hundreds of megabytes of
mostly-repeated context. Oversized payloads are written with their bulk replaced
by a marker: what the agent did stays readable forever, and the full prompt stays
live only while the process that made it is running.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from ..events import Event

FILENAME = "events.jsonl"

#: Per event. Comfortably fits a tool call with its output, a diff, a step
#: transition — everything except a full prompt.
MAX_EVENT_BYTES = 96_000
#: Per session. A run that produces more than this has already been read.
MAX_FILE_BYTES = 200 * 1024 * 1024

#: Payload fields big enough to be worth dropping on their own, in the order
#: they are given up.
_HEAVY = ("messages", "rendered", "raw", "diff", "output", "result")


def _shrink(payload: dict) -> dict:
    """Drop the bulky parts of an oversized payload, keeping its shape."""
    trimmed = dict(payload)
    for key in _HEAVY:
        if key not in trimmed:
            continue
        original = trimmed[key]
        size = len(json.dumps(original, default=str))
        trimmed[key] = (f"[{size:,} bytes not kept on disk — the full value was "
                        f"available live, in this run's session]")
        trimmed.setdefault("truncated_on_disk", []).append(key)
        if len(json.dumps(trimmed, default=str)) <= MAX_EVENT_BYTES:
            break
    return trimmed


class SessionLog:
    """Append-only event trace for one session."""

    def __init__(self, directory: Path):
        self.path = Path(directory) / FILENAME
        self._lock = threading.Lock()
        self._full = False

    def append(self, event: Event) -> None:
        data = event.to_dict()
        line = json.dumps(data, default=str)
        if len(line) > MAX_EVENT_BYTES:
            data["payload"] = _shrink(data.get("payload") or {})
            line = json.dumps(data, default=str)

        with self._lock:
            if self._full:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > MAX_FILE_BYTES:
                    self._full = True
                    return
                with self.path.open("a", encoding="utf8") as handle:
                    handle.write(line + "\n")
            except OSError:
                # A trace that cannot be written is not a reason to stop a run.
                self._full = True

    def read(self) -> list[Event]:
        """Every event recorded for this session, oldest first."""
        try:
            text = self.path.read_text(encoding="utf8")
        except OSError:
            return []
        events: list[Event] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue          # a torn last line from a kill mid-write
            known = {f for f in Event.__dataclass_fields__}
            events.append(Event(**{k: v for k, v in data.items() if k in known}))
        return events

    def delete(self) -> None:
        with self._lock:
            self.path.unlink(missing_ok=True)
