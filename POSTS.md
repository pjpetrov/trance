# Announcement drafts (not committed — paste and delete)

## 1. r/LocalLLaMA — post first, attach docs/screens/gta2-history.png

**Title:** A coding-agent harness built on transparency: you see everything
the agents do, and if you don't like where it went, you rewind and continue
from the point you liked — local Qwen 27B on one RTX 3090

Most agent harnesses are a black box that ends in "done ✓". I built **trance**
around the opposite idea: you can see *everything* your agents do, and nothing
they claim is taken on trust.

Every model call is in the history with the exact prompt it was sent. Every
command, every file edit, every verdict links to its evidence. And when a step
says "the game works", it has to prove it: a visual tester opens the build in
a real headless Chrome, clicks the menu, presses WASD, films the screen, and a
vision model judges the pixels — the frame it saw and the verdict it gave are
right there in the history, openable. When it says the character didn't move,
you can look at exactly what it looked at. Failures route back to the
developer agent with the screenshots attached.

And because you can see everything, you can also *undo* anything. Every step
is one git commit — revert it with a click, re-apply if you change your mind.
The History page shows each request you made with its plan, its commits and
its screenshots — and any of them is a point you can go back to: **rewind the
project to where an iteration ended and continue from there** (the abandoned
work stays on a branch), or serve any old version in the browser to find
where a bug crept in.

It is all tuned for small local models: a tree-sitter call graph feeds agents
individual symbols instead of whole files (a 33KB file is ~8,400 tokens; the
function you need is 150), and each agent has a path remit it cannot write
outside of. The games in the screenshots — a GTA-style driving game, an RTS,
worms — were built and end-to-end tested by Qwen 27B with a 64k window on a
single 3090. Plans are editable and nothing runs until you press Run.

Needs Python 3.11+, git, Chrome, and any OpenAI-compatible endpoint
(llama.cpp / Ollama). Anthropic API and Claude Code work as backends too.

It is early and I would genuinely like it kicked: does the visual loop
survive contact with *your* projects, and where does setup fight you?

https://github.com/pjpetrov/trance

## 2. Show HN — a few days after, if the first lands well

**Title:** Show HN: Trance – an agent harness where you see everything, and
can rewind to any point you liked

Trance is a self-hosted multi-agent coding harness built on two ideas.

First, transparency: the agent's report is not evidence. Every prompt,
command, diff and verdict is in an inspectable history — and "it works" has
to be shown, not said: a visual tester drives the built app in headless
Chrome (keys, clicks, frame captures) and a vision model judges the pixels,
with every frame and verdict stored. Failures go back to the developer agent
with the screenshots.

Second, reversibility: because everything is visible, everything is undoable.
One git commit per step with one-click revert; a timeline of every request
with its plan, commits and screenshots; and any iteration is a restore point —
rewind the project to where it ended and continue from there (the abandoned
tip stays on a branch), or serve that exact old version in the browser to
bisect a regression.

It is tuned for small local models — a call-graph index feeds symbols rather
than files, so 64k-window models build real projects — and was built by
testing it on real games (a GTA clone, an RTS, worms) with Qwen 27B on one
RTX 3090. Feedback wanted, especially on where the visual tester's judgment
breaks. https://github.com/pjpetrov/trance

## 3. Short — X / Mastodon / Discord (llama.cpp, KoboldAI servers)

A coding-agent harness where you see everything: every prompt, every diff,
every screenshot the visual tester judged in a real browser — and if you
don't like where it went, you rewind to the iteration you liked and continue
from there. Runs on Qwen 27B, one RTX 3090. Looking for testers:
https://github.com/pjpetrov/trance

## Posting notes

- Make the repo public and `git push` first; check the README screenshots
  render on the GitHub page.
- Post from your own account, answer comments quickly for the first two
  hours — that is what keeps a thread alive.
- Expect blunt feedback about install friction; that is the useful kind.
- Do not post the HN one the same day; HN punishes reposts of things it has
  seen flop, so let the Reddit thread teach you what confuses people first.
