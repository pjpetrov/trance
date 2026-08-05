"""Event bus.

Every observable thing — a model call with its full prompt, a tool call, a step
transition, a steering note — becomes an Event. Two subscribers matter: the
trace writer (durable, on disk) and the WebSocket fan-out (live, to the UI).

The engine runs on a worker thread and the server on an asyncio loop, so
publishing is thread-safe and delivery to async subscribers hops the loop.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

MAX_SUMMARY_CHARS = 400


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class Event:
    type: str
    session_id: str
    id: str = field(default_factory=lambda: f"ev_{uuid.uuid4().hex[:12]}")
    ts: str = field(default_factory=_now)
    agent: str | None = None
    step_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class EventBus:
    """Fan-out to sync and async subscribers, safe to publish from any thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sync: list[Callable[[Event], None]] = []
        self._queues: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._history: dict[str, list[Event]] = {}

    def subscribe_sync(self, handler: Callable[[Event], None]) -> None:
        with self._lock:
            self._sync.append(handler)

    def subscribe_async(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._queues.append((loop, queue))
        return queue

    def unsubscribe_async(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._queues = [(l, q) for l, q in self._queues if q is not queue]

    def publish(self, event: Event) -> Event:
        with self._lock:
            self._history.setdefault(event.session_id, []).append(event)
            sync = list(self._sync)
            queues = list(self._queues)

        for handler in sync:
            try:
                handler(event)
            except Exception:  # a broken subscriber must not kill the run
                pass
        for loop, queue in queues:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                pass  # loop closed; the websocket cleanup will drop it
        return event

    def emit(self, type: str, session_id: str, **fields: Any) -> Event:
        return self.publish(Event(type=type, session_id=session_id, **fields))

    def history(self, session_id: str) -> list[Event]:
        with self._lock:
            return list(self._history.get(session_id, []))


def summarize_messages(messages: list[dict]) -> dict:
    """Collapsed view of a prompt, so the UI can show a one-liner by default.

    The full messages always travel with the event — this is only what shows
    before the user expands it.
    """
    total_chars = sum(len(str(m.get("content") or "")) for m in messages)
    system = next((m for m in messages if m.get("role") == "system"), None)
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    tool_results = [m for m in messages if m.get("role") == "tool"]
    return {
        "message_count": len(messages),
        "chars": total_chars,
        "est_tokens": max(1, total_chars // 4),
        "system_preview": _clip(system.get("content") if system else ""),
        "user_preview": _clip(last_user.get("content") if last_user else ""),
        "tool_result_count": len(tool_results),
        "roles": [m.get("role") for m in messages],
    }


def _clip(text: Any, limit: int = MAX_SUMMARY_CHARS) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + f"… (+{len(text) - limit} chars)"
