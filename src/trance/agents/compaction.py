"""Context compaction for long agent turns — PI's design, carried over.

Copied deliberately from the PI harness (github.com/earendil-works/pi,
`core/compaction/`), which proved the approach on the same local model this
runs against: when a conversation nears the window, keep the newest ~20k
tokens verbatim, have the model itself write a structured checkpoint summary
of everything older, and splice that summary in as a user message. The
prompts, the constants, the cut rules and the deterministic file lists are
PI's; only the message shapes are trance's (OpenAI dicts, not PI's blocks).

Pure functions here; the runner makes the model call and emits the events —
the same split PI keeps between its compaction module and session manager.
"""

from __future__ import annotations

import json

#: PI's DEFAULT_COMPACTION_SETTINGS, verbatim. `reserve` is the room kept
#: free at the top of the window — the summary generation itself may use up
#: to 80% of it. `keep_recent` is how much of the newest conversation
#: survives verbatim.
RESERVE_TOKENS = 16384
KEEP_RECENT_TOKENS = 20000
#: A tool result longer than this is clipped in the *serialized* text handed
#: to the summarizer. The conversation itself is never touched by this.
TOOL_RESULT_MAX_CHARS = 2000
#: Never cut into the head of the conversation: the system prompt and the
#: task are the two messages fit_context also refuses to drop.
HEAD = 2

SYSTEM_PROMPT = (
    "You are a context summarization assistant. Your task is to read a "
    "conversation between a user and an AI assistant, then produce a "
    "structured summary following the exact format specified.\n\n"
    "Do NOT continue the conversation. Do NOT respond to any questions in "
    "the conversation. ONLY output the structured summary."
)

SUMMARIZATION_PROMPT = """The messages above are a conversation to summarize. Create a structured context checkpoint summary that another LLM will use to continue the work.

Use this EXACT format:

## Goal
[What is the user trying to accomplish? Can be multiple items if the session covers different tasks.]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned by user]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Any data, examples, or references needed to continue]
- [Or "(none)" if not applicable]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

UPDATE_SUMMARIZATION_PROMPT = """The messages above are NEW conversation messages to incorporate into the existing summary provided in <previous-summary> tags.

Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context from the new messages
- UPDATE the Progress section: move items from "In Progress" to "Done" when completed
- UPDATE "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, and error messages
- If something is no longer relevant, you may remove it

Use this EXACT format:

## Goal
[Preserve existing goals, add new ones if the task expanded]

## Constraints & Preferences
- [Preserve existing, add new ones discovered]

## Progress
### Done
- [x] [Include previously done items AND newly completed items]

### In Progress
- [ ] [Current work - update based on progress]

### Blocked
- [Current blockers - remove if resolved]

## Key Decisions
- **[Decision]**: [Brief rationale] (preserve all previous, add new)

## Next Steps
1. [Update based on current state]

## Critical Context
- [Preserve important context, add new if needed]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

SUMMARY_PREFIX = (
    "The conversation history before this point was compacted into the "
    "following summary:\n\n<summary>\n"
)
SUMMARY_SUFFIX = "\n</summary>"

#: Which trance tools touch which list. PI keys on its read/write/edit tools;
#: these are ours.
_READ_TOOLS = frozenset({"read_file"})
_WRITE_TOOLS = frozenset({"write_file", "append_file", "edit_file", "replace_symbol"})


def threshold(config) -> int:
    """PI's trigger point, in trance's budget terms.

    PI compacts when the context passes `window - reserve`, the reserve
    covering both the next reply and the summary generation. Trance's
    input_budget already subtracts the reply room, so the threshold is
    whichever is lower: the input budget, or the window less PI's reserve.
    """
    return min(config.input_budget,
               int(config.context_window or 0) - RESERVE_TOKENS)


def should_compact(tokens: int, config) -> bool:
    return tokens > threshold(config)


def keep_recent_tokens(config) -> int:
    """How much of the newest conversation survives verbatim.

    PI's flat 20,000 assumes the 200k windows it usually runs against, where
    it is a sliver. On a 64k window it left only ~17k of headroom per cycle,
    and an agent generating 2-8k tokens a round folded every five minutes —
    85 folds in one measured day, an hour of GPU spent summarizing. The tail
    scales instead: a quarter of the trigger point, capped at PI's 20k — so
    a 200k window behaves exactly like PI, and a 64k one keeps ~11.7k
    verbatim and folds half as often, twice as deep.
    """
    return min(KEEP_RECENT_TOKENS, threshold(config) // 4)


def find_cut(messages: list[dict], chars_per_token: float,
             keep_recent: int = KEEP_RECENT_TOKENS) -> int:
    """The index the verbatim tail starts at.

    PI's walk: backwards from the newest message, accumulating estimated
    sizes, stopping once ~keep_recent tokens are held — then cut at the
    nearest user or assistant message at or after that point, never at a
    tool result (it must stay behind its call). Returns HEAD when there is
    nothing worth summarizing behind the tail.
    """
    held = 0.0
    cut = HEAD
    for index in range(len(messages) - 1, HEAD - 1, -1):
        held += _size(messages[index]) / max(chars_per_token, 1.0)
        if held >= keep_recent:
            cut = index
            break
    else:
        return HEAD
    while cut < len(messages) and messages[cut].get("role") == "tool":
        cut += 1
    return cut


def previous_summary(messages: list[dict]) -> str:
    """The summary a prior compaction left, if any — for the rolling update."""
    for message in messages:
        content = message.get("content") or ""
        if message.get("role") == "user" and isinstance(content, str) \
                and content.startswith(SUMMARY_PREFIX):
            inner = content[len(SUMMARY_PREFIX):]
            end = inner.rfind("</summary>")
            return inner[:end].rstrip() if end >= 0 else inner
    return ""


def serialize_conversation(messages: list[dict]) -> str:
    """PI's serialization: the conversation as labelled text, so the model
    summarizes it rather than continuing it. Tool results are clipped —
    their full content is not needed to summarize them."""
    parts: list[str] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        text = content if isinstance(content, str) else _image_text(content)
        if role == "user" and text:
            parts.append(f"[User]: {text}")
        elif role == "assistant":
            thinking = message.get("reasoning_content") or ""
            if thinking:
                parts.append(f"[Assistant thinking]: {thinking}")
            if text:
                parts.append(f"[Assistant]: {text}")
            calls = [_call_line(c) for c in message.get("tool_calls") or []]
            if calls:
                parts.append(f"[Assistant tool calls]: {'; '.join(calls)}")
        elif role == "tool" and text:
            parts.append(f"[Tool result]: {_clip(text, TOOL_RESULT_MAX_CHARS)}")
    return "\n\n".join(parts)


def summarization_request(serialized: str, prior: str = "") -> list[dict]:
    """The standalone conversation the summary is asked for in."""
    prompt = f"<conversation>\n{serialized}\n</conversation>\n\n"
    if prior:
        prompt += f"<previous-summary>\n{prior}\n</previous-summary>\n\n"
    prompt += UPDATE_SUMMARIZATION_PROMPT if prior else SUMMARIZATION_PROMPT
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt}]


def summary_output_tokens(config) -> int:
    """PI: the summary may spend up to 80% of the reserve, capped by the
    model's own output limit."""
    limit = int(config.max_tokens or 0)
    budget = int(0.8 * RESERVE_TOKENS)
    return min(budget, limit) if limit > 0 else budget


def file_operations(messages: list[dict]) -> tuple[set[str], set[str]]:
    """(read, modified) paths named in tool calls — tracked mechanically,
    like PI, so what was touched never depends on the summary mentioning it.
    A previous compaction's lists ride in via its summary message."""
    read: set[str] = set()
    modified: set[str] = set()
    for message in messages:
        content = message.get("content") or ""
        if message.get("role") == "user" and isinstance(content, str) \
                and content.startswith(SUMMARY_PREFIX):
            prior_read, prior_modified = _parse_file_lists(content)
            read |= prior_read
            modified |= prior_modified
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                continue
            path = args.get("path")
            if not isinstance(path, str) or not path:
                continue
            if fn.get("name") in _READ_TOOLS:
                read.add(path)
            elif fn.get("name") in _WRITE_TOOLS:
                modified.add(path)
    return read, modified


def format_file_operations(read: set[str], modified: set[str]) -> str:
    """PI's XML tail: files only read, and files changed."""
    read_only = sorted(read - modified)
    changed = sorted(modified)
    sections = []
    if read_only:
        sections.append("<read-files>\n" + "\n".join(read_only) + "\n</read-files>")
    if changed:
        sections.append("<modified-files>\n" + "\n".join(changed) + "\n</modified-files>")
    return "\n\n" + "\n\n".join(sections) if sections else ""


def summary_message(summary: str, files_xml: str = "") -> dict:
    """The compacted history, as the model will see it from now on."""
    return {"role": "user", "content": SUMMARY_PREFIX + summary + files_xml + SUMMARY_SUFFIX}


def splice(messages: list[dict], cut: int, summary: str, files_xml: str = "") -> list[dict]:
    """head + summary + verbatim tail: the compacted conversation."""
    return messages[:HEAD] + [summary_message(summary, files_xml)] + messages[cut:]


def _size(message: dict) -> int:
    content = message.get("content")
    chars = len(content) if isinstance(content, str) else len(_image_text(content))
    chars += len(message.get("reasoning_content") or "")
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        chars += len(fn.get("name") or "") + len(fn.get("arguments") or "")
    return chars


def _image_text(content) -> str:
    """The text parts of a multi-part message; an image counts as PI counts
    it, roughly 4,800 characters."""
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
        elif isinstance(block, dict) and block.get("type") == "image_url":
            parts.append(" " * 4800)
    return "".join(parts)


def _call_line(call: dict) -> str:
    fn = call.get("function") or {}
    raw = fn.get("arguments") or "{}"
    try:
        args = json.loads(raw)
        rendered = ", ".join(f"{k}={json.dumps(v)}" for k, v in args.items())
    except (json.JSONDecodeError, AttributeError):
        rendered = _clip(str(raw), 200)
    return f"{fn.get('name') or 'tool'}({rendered})"


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n\n[... {len(text) - limit} more characters truncated]"


def _parse_file_lists(content: str) -> tuple[set[str], set[str]]:
    def tag(name: str) -> set[str]:
        start = content.find(f"<{name}>")
        end = content.find(f"</{name}>")
        if start < 0 or end < 0:
            return set()
        inner = content[start + len(name) + 2 : end]
        return {line.strip() for line in inner.splitlines() if line.strip()}

    return tag("read-files"), tag("modified-files")
