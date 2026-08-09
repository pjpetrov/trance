"""Ask a multimodal model a question about a picture.

One call, one image, one answer, sent to the asking agent's own model. That is
the only model involved: an agent given the browser toolset needs one that can
see, and which model that is belongs to the agent rather than to a separate
global setting nobody would remember to keep in step with it.

The prompt shape here is the whole defence against a confident wrong answer.
Asked "does this look right?", a vision model will tell you about a button that
is not there — measured, on this machine: with the image withheld entirely, the
local model still described "a blue circle" for an image that was a yellow one.
So every question is asked with the evidence demanded alongside the answer, and
a description is required *before* the judgement, so a hallucinated detail has
to be written down where you can see it rather than only implied by a verdict.
"""

from __future__ import annotations

import base64
from dataclasses import replace

from .config import ModelConfig
from .providers import client_for

#: Kinds that can be handed an image. `claudecode` runs a CLI that takes a
#: prompt string, so there is nowhere to put one.
VISION_KINDS = ("llamacpp", "openai", "ollama", "vllm", "openrouter", "deepseek", "anthropic")

#: Backends whose chat template takes `enable_thinking`. Only these get it: a
#: strict gateway answers 400 to a body field it does not know.
THINKING_TOGGLE_KINDS = ("llamacpp", "vllm")

#: Describing one picture does not need a chain of thought, and on a reasoning
#: model it is actively harmful here — measured against the local Qwen, the
#: whole output budget went to thinking and the answer came back empty with
#: finish_reason=length. Off, the same question answered correctly in 10s.
NO_THINKING = {"chat_template_kwargs": {"enable_thinking": False}}

#: Floor for the answer. A description plus per-check evidence does not fit in
#: the few hundred tokens a preset might carry for short completions.
MIN_ANSWER_TOKENS = 700

PREAMBLE = """You are inspecting a screenshot of a running web application to find visual defects.

Answer in exactly this order:

1. DESCRIBE — what you can actually see: each distinct element and roughly where
   it is. Describe only what is in the image.
2. CHECKS — answer each question below with YES or NO, and after each one state
   the evidence in the image that made you answer that way.
3. ANSWER — one or two sentences summarising whether what you see is acceptable.

If you cannot see something, say that you cannot see it. Do not assume an
element is present because the application would normally have one."""


class VisionUnavailable(RuntimeError):
    """No usable vision model configured. The caller degrades to the cheap probes."""


def image_block(png: bytes, kind: str) -> dict:
    """One image, in whichever shape this API speaks. Shared with the chat, so
    a screenshot pasted into the conversation travels the same way a visual
    check's does."""
    encoded = base64.b64encode(png).decode("ascii")
    if kind == "anthropic":
        return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                            "data": encoded}}
    return {"type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{encoded}"}}


def _content(png: bytes, prompt: str, kind: str) -> list[dict]:
    """The image and the question, in whichever shape this API speaks."""
    encoded = base64.b64encode(png).decode("ascii")
    if kind == "anthropic":
        return [
            {"type": "text", "text": prompt},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": encoded}},
        ]
    return [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
    ]


def question_prompt(question: str, checks: list[str] | None = None) -> str:
    """The agent's question, wrapped in the shape that keeps answers honest."""
    parts = [PREAMBLE, "", "The question you are answering:", question.strip()]
    if checks:
        parts += ["", "Check each of these specifically:"]
        parts += [f"  {n}. {check}" for n, check in enumerate(checks, 1)]
    return "\n".join(parts)


def look(png: bytes, question: str, config: ModelConfig, *, checks: list[str] | None = None,
         cancel_token: str = "") -> dict:
    """Send one screenshot and one question. Returns the answer and what it cost."""
    kind = getattr(config, "kind", "") or "llamacpp"
    if kind not in VISION_KINDS:
        raise VisionUnavailable(
            f"this agent's model ({config.preset or config.model}) speaks {kind!r}, which "
            "cannot be sent an image. Give the agent a multimodal model to use the "
            "browser toolset's look tool.")
    if not png:
        raise VisionUnavailable("there is no screenshot to look at")

    prompt = question_prompt(question, checks)
    if config.max_tokens < MIN_ANSWER_TOKENS:
        config = replace(config, max_tokens=MIN_ANSWER_TOKENS)
    client = client_for(config)
    extra = dict(NO_THINKING) if kind in THINKING_TOGGLE_KINDS else None
    kwargs = {"cancel_token": cancel_token}
    if extra:
        kwargs["extra_body"] = extra
    response = client.complete(
        [{"role": "user", "content": _content(png, prompt, kind)}], **kwargs)
    text = (response.text or "").strip()
    if not text:
        # A model that answered with nothing has not judged anything, and
        # reporting an empty string as its opinion reads as "no problems found".
        raise VisionUnavailable(
            "the vision model returned an empty answer"
            + (f" (finish_reason={response.finish_reason})" if response.finish_reason else "")
            + (". Its whole output budget went to reasoning — raise max_tokens on this "
               "model, or use one that can be told not to think."
               if response.finish_reason == "length" else ""))
    return {
        "answer": text,
        "prompt": prompt,
        "model": config.model,
        "preset": config.preset,
        "usage": dict(getattr(response, "usage", None) or {}),
    }
