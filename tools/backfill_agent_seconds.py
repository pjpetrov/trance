"""Reconstruct per-agent working time from the event trace.

Per-agent charging arrived later than the sessions it should have counted:
a 10-hour project showed 90 attributed minutes, because the first eight and
a half hours ran before the ledger existed. The events were always there,
though — every model call, tool call and verdict, stamped with an agent and
a time — so the attribution can be rebuilt: within a run, the gap between
two events belongs to whoever produced the *next* one; they were working up
to it. Time before an engine-owned event (indexing, curation) stays
unattributed, exactly as live charging leaves it.

Usage, with the server stopped (it rewrites session.json on touch):

    python tools/backfill_agent_seconds.py ~/trance_workspace [more roots...]

Only sessions whose attributed total falls well short of their run clock are
touched, and only when the reconstruction accounts for more than the stored
ledger does.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

#: Events that mean "a run is in progress" / "it ended".
RUN_STARTS = {"run_started", "step_run_started"}
RUN_ENDS = {"run_finished", "run_stopped", "run_halted"}
#: Agent names that are not agents for charging purposes.
NOBODY = {None, "", "you", "system", "orchestrator"}


def _ts(raw: str) -> float:
    return datetime.fromisoformat(raw).timestamp()


def reconstruct(events_path: Path) -> dict[str, float]:
    seconds: dict[str, float] = {}
    in_run = False
    last: float | None = None
    with events_path.open(encoding="utf8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind, when = event.get("type"), event.get("ts")
            if not when:
                continue
            now = _ts(when)
            if kind in RUN_STARTS:
                in_run, last = True, now
                continue
            if kind in RUN_ENDS:
                in_run, last = False, None
                continue
            if in_run and last is not None:
                gap = now - last
                agent = event.get("agent")
                # A day-long hole mid-run is a crash nobody stopped the clock
                # for, not work. Delegated steps legitimately go an hour.
                if 0 < gap < 2 * 3600 and agent not in NOBODY:
                    seconds[agent] = seconds.get(agent, 0.0) + gap
            last = now
    return seconds


def backfill(root: Path) -> None:
    for session_file in sorted(root.glob("*/.trance/sessions/*/session.json")):
        events = session_file.parent / "events.jsonl"
        if not events.exists():
            continue
        data = json.loads(session_file.read_text(encoding="utf8"))
        run_seconds = float(data.get("run_seconds") or 0)
        stored = data.get("agent_seconds") or {}
        if run_seconds <= 0 or sum(stored.values()) >= 0.8 * run_seconds:
            continue                       # already accounted for
        rebuilt = reconstruct(events)
        if sum(rebuilt.values()) <= sum(stored.values()):
            continue                       # the ledger already knows more
        data["agent_seconds"] = {k: round(v, 1) for k, v in rebuilt.items()}
        session_file.write_text(json.dumps(data, indent=2), encoding="utf8")
        print(f"{session_file.parent.parent.parent.parent.name}"
              f"/{session_file.parents[3].name}: "
              f"{sum(stored.values()) / 60:.0f}m -> {sum(rebuilt.values()) / 60:.0f}m "
              f"attributed (clock {run_seconds / 60:.0f}m)")


if __name__ == "__main__":
    roots = [Path(p).expanduser() for p in sys.argv[1:]] or [Path.cwd()]
    for r in roots:
        backfill(r)
