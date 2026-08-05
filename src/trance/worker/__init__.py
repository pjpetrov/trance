"""Worker agent — receives a curated bundle and does the task.

The design decision that matters here: the worker can *pull* context lazily via
graph-backed tools, so an under-fetching curator costs one tool round trip
instead of producing a hallucination. Every tool call is traced with `hit` and
`result_tokens`; a high tool-call rate is the signal that the curator's
`max_hops` or `token_budget` is too tight for that shape of task.

Model choice: this is the expensive agent, so it gets the capable model. The
curator stays heuristic and needs no LLM at all — see trance/curator/walker.py.
"""

from __future__ import annotations

from .agent import SYSTEM_PROMPT, ToolInvocation, WorkerResult, run
from .client import BackendError, ChatClient, ChatResponse
from .tools import ContextTools, specs as tool_specs

__all__ = [
    "run",
    "WorkerResult",
    "ToolInvocation",
    "SYSTEM_PROMPT",
    "ChatClient",
    "ChatResponse",
    "BackendError",
    "ContextTools",
    "tool_specs",
]
