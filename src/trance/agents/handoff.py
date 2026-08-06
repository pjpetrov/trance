"""What one agent hands to the next when a step fails.

A fixer that is only told "the tests failed" has to rediscover the failure
before it can fix anything: run the suite again, read the files, guess which
assertion broke. The agent that just ran already has all of that, and throwing
it away is the expensive kind of context minimisation — it saves tokens on the
handoff and spends far more re-deriving them.

So the failing agent's turn is replayed for the fixer: the commands it ran with
their output, the edits it made with their diffs, and its own closing report.
What is *not* replayed is file contents it read — those the fixer can pull
itself, and they are the bulk of a transcript. That is the whole trade: keep
what was learned, drop what can be re-fetched.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Roughly 3k tokens. Enough for a failing test run plus the diffs around it.
DEFAULT_BUDGET = 12_000

#: Kept when the budget bites, most valuable last to go.
P_READ, P_ROUTINE, P_EDIT, P_FAILURE = 0, 1, 2, 3

_LOOKUPS = {"get_definition", "get_callers", "get_callees", "search_symbols",
            "list_files", "read_file", "check_file", "check_files"}


@dataclass
class Moment:
    """One thing that happened, with the part worth reprinting kept separate."""

    head: str
    body: str = ""
    priority: int = P_ROUTINE
    #: Reads are named but never quoted, so their bodies are dropped up front.
    quotable: bool = True

    def render(self) -> str:
        if self.body and self.quotable:
            return f"{self.head}\n{_fence(self.body)}"
        return self.head


def _fence(text: str) -> str:
    return "```\n" + text.rstrip() + "\n```"


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    # Both ends: a stack trace starts with the failure and ends with the summary.
    half = limit // 2
    return f"{text[:half]}\n… [{len(text) - limit} chars omitted] …\n{text[-half:]}"


def _moment(entry: dict) -> Moment:
    detail = entry.get("detail") or {}
    kind = detail.get("kind") or ""
    tool = entry.get("tool") or "?"
    args = entry.get("arguments") or {}
    ok = entry.get("ok", True)

    if kind == "command":
        failed = detail.get("exit_code") != 0 or detail.get("timed_out") or detail.get("cancelled")
        status = ("timed out" if detail.get("timed_out")
                  else "cancelled" if detail.get("cancelled")
                  else f"exit {detail.get('exit_code')}")
        return Moment(
            head=f"$ {detail.get('command', '')}   → {status}",
            body=_clip(detail.get("output") or "", 4000),
            priority=P_FAILURE if failed else P_ROUTINE,
        )

    if kind == "write":
        sign = "created" if detail.get("created") else "edited"
        return Moment(
            head=(f"{sign} {detail.get('path', '')}  "
                  f"+{detail.get('added', 0)} −{detail.get('removed', 0)}"),
            body=_clip(detail.get("diff") or "", 3000),
            priority=P_EDIT,
        )

    if kind in ("truncated", "malformed"):
        return Moment(head=f"{tool}: call was cut off and never ran", priority=P_EDIT)

    if not ok:
        return Moment(head=f"{tool} refused", body=_clip(entry.get("text") or "", 600),
                      priority=P_FAILURE)

    if tool in _LOOKUPS:
        what = detail.get("path") or next((str(v) for v in args.values()), "")
        return Moment(head=f"looked at {what}".rstrip(), priority=P_READ, quotable=False)

    shown = ", ".join(f"{k}={_clip(str(v), 40)}" for k, v in args.items())
    return Moment(head=f"{tool}({shown})", body=_clip(entry.get("text") or "", 800))


def digest(transcript: list[dict], final_text: str = "",
           *, budget_chars: int = DEFAULT_BUDGET) -> str:
    """Replay a turn for whoever has to act on it, inside a char budget."""
    moments = [_moment(entry) for entry in transcript or []]
    if not moments and not final_text:
        return ""

    tail = final_text.strip()
    fixed = len(tail) + 40                     # the report itself is never trimmed

    def total() -> int:
        return sum(len(m.render()) + 2 for m in moments) + fixed

    # Give up bodies before whole moments: knowing a command ran at all is worth
    # more than the tail of its output. Least valuable and oldest go first.
    for level in (P_READ, P_ROUTINE, P_EDIT, P_FAILURE):
        for moment in moments:
            if total() <= budget_chars:
                break
            if moment.priority == level and moment.body:
                moment.body = ""
    while len(moments) > 1 and total() > budget_chars:
        moments.pop(0)                         # then drop the oldest outright

    lines = [m.render() for m in moments]
    out = "\n\n".join(lines)
    if tail:
        out = (out + "\n\n" if out else "") + "Its closing report:\n" + tail
    return out


@dataclass
class Handoff:
    """The bundle a fixer receives, and what the UI shows was handed over."""

    body: str
    moments: int = 0
    chars: int = field(default=0)

    def to_dict(self) -> dict:
        return {"body": self.body, "moments": self.moments, "chars": self.chars}


def build(transcript: list[dict], final_text: str = "",
          *, budget_chars: int = DEFAULT_BUDGET) -> Handoff:
    body = digest(transcript, final_text, budget_chars=budget_chars)
    return Handoff(body=body, moments=len(transcript or []), chars=len(body))
