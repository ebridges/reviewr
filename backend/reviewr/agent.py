"""Run a coding agent in a draft worktree to make a work item's changes.

Claude: the Claude Agent SDK, with file tools only (no shell).
Codex: `codex exec` in its workspace-write sandbox, which confines writes to the
worktree and has no network access; unlike the Claude setup it can run commands there.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import tempfile
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query

from reviewr import ai
from reviewr.config import load_config
from reviewr.models import PullRequest
from reviewr.state import ReviewState, WorkItem

# File tools only: the agent reads and edits code in its own worktree, but can't run
# commands, so it can't push, install packages, or touch anything outside it.
TOOLS = ["Read", "Edit", "Write", "Glob", "Grep"]


class AgentError(RuntimeError):
    pass


_TAG = re.compile(r"<[^>]+>")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_IMG = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
# Footers review bots add to every comment; they say nothing about the code.
_BOILERPLATE = re.compile(r"^\s*Useful\? React with .*$", re.MULTILINE)


def _readable(body: str) -> str:
    """A review comment without HTML, image badges (kept as their label, e.g. "P2"), or
    bot footers, which only make the prompt harder to read."""
    text = _HTML_COMMENT.sub("", body)
    text = _IMG.sub(lambda m: re.sub(r"\s*badge$", "", m.group(1), flags=re.IGNORECASE), text)
    text = _TAG.sub("", text)
    text = _BOILERPLATE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _code(diff_hunk: str, lines: int = 20) -> str:
    """The end of the diff hunk GitHub shows with the comment: the code it's attached to."""
    rows = diff_hunk.strip().splitlines()
    return "\n".join(rows[-lines:])


def agent_prompt(pr: PullRequest, state: ReviewState, item: WorkItem) -> str:
    """The prompt for drafting a work item. Shown on the Fix page, where you can edit it;
    an edited prompt is stored on the item and used instead."""
    parts = [
        (
            f'You are making changes to address code review comments on the pull request "{pr.title}". '
            "The repository is checked out in the current directory at the PR's branch."
        ),
        "",
        f"Work item: {item.title}",
        "",
        (
            "Make the smallest change that does what each reviewer asked. Don't change unrelated code, "
            "don't reformat files, and don't commit. When you're done, reply with one short paragraph "
            "saying what you changed."
        ),
    ]
    for n, tid in enumerate(item.thread_ids, 1):
        t = pr.thread(tid)
        d = state.decisions.get(tid)
        parts += ["", f"## Comment {n}: {t.path}, line {t.line or t.original_line}"]
        if t.diff_hunk:
            parts += ["", "The code it's attached to (the last line is the one commented on):", "```diff", _code(t.diff_hunk), "```"]
        parts.append("")
        for c in t.comments:
            parts.append(f"{c.author}:")
            parts.append(_readable(c.body))
            parts.append("")
        if d and d.note:
            parts.append(f"The author's note on this comment: {d.note}")
    if item.instruction:
        parts += ["", f"Additional instruction from the author: {item.instruction}"]
    return "\n".join(parts).rstrip() + "\n"


def prompt_for(pr: PullRequest, state: ReviewState, item: WorkItem) -> str:
    """The prompt a draft uses: yours if you edited it, otherwise the default."""
    return item.prompt or agent_prompt(pr, state, item)


async def _run(worktree: Path, prompt: str, model: str) -> str:
    log: list[str] = []
    options = ClaudeAgentOptions(
        cwd=str(worktree),
        tools=TOOLS,
        allowed_tools=TOOLS,
        permission_mode="acceptEdits",
        model=model,
        max_turns=80,
        setting_sources=["project"],  # pick up the repo's CLAUDE.md conventions
    )
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            log.extend(b.text for b in message.content if isinstance(b, TextBlock))
        elif isinstance(message, ResultMessage) and message.is_error:
            raise AgentError(message.result or f"Agent stopped: {message.subtype}")
    return "\n\n".join(log)


CODEX_TIMEOUT_S = 1800


def _run_codex(worktree: Path, prompt: str) -> str:
    with tempfile.TemporaryDirectory(prefix="reviewr-codex-agent-") as tmp:
        last = Path(tmp) / "last.txt"
        argv = [
            "codex", "exec", "--skip-git-repo-check", "--ephemeral", "--color", "never",
            "-s", "workspace-write", "-C", str(worktree), "-o", str(last),
        ]
        if model := load_config().ai.codex_model:
            argv += ["-m", model]
        try:
            proc = subprocess.run(argv + ["-"], input=prompt, capture_output=True, text=True, timeout=CODEX_TIMEOUT_S, check=False)
        except FileNotFoundError as e:
            raise AgentError("The Codex CLI isn't installed. Install it and run `codex login`, or switch to Claude.") from e
        except subprocess.TimeoutExpired as e:
            raise AgentError(f"Codex didn't finish within {CODEX_TIMEOUT_S // 60} minutes.") from e
        if proc.returncode != 0:
            raise AgentError(f"Codex failed (exit {proc.returncode}): {(proc.stderr or proc.stdout).strip()[-600:]}")
        return last.read_text() if last.exists() else ""


def run_agent(worktree: Path, prompt: str, model: str) -> str:
    """Draft changes in `worktree` with the current provider. `model` is the Claude model."""
    if ai.current() == "codex":
        return _run_codex(worktree, prompt)
    return asyncio.run(_run(worktree, prompt, model))
