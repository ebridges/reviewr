"""Run a coding agent in a draft worktree to make a work item's changes.

Claude: the Claude Agent SDK, with file tools only (no shell).
Codex: `codex exec` in its workspace-write sandbox, which confines writes to the
worktree and has no network access; unlike the Claude setup it can run commands there.
"""

from __future__ import annotations

import asyncio
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


def agent_prompt(pr: PullRequest, state: ReviewState, item: WorkItem) -> str:
    parts = [
        (
            f'You are making changes to address code review comments on the pull request "{pr.title}". '
            "The repository is checked out in the current directory at the PR's branch."
        ),
        "",
        f"Work item: {item.title}",
        "",
        (
            "Make the smallest change that does what each reviewer asked. Don't change unrelated "
            "code, don't reformat files, and don't commit. When you're done, reply with one short "
            "paragraph saying what you changed."
        ),
        "",
    ]
    for tid in item.thread_ids:
        t = pr.thread(tid)
        d = state.decisions.get(tid)
        convo = "\n".join(f"  {c.author}: {c.body.strip()}" for c in t.comments)
        parts.append(f"Comment on {t.path} line {t.line or t.original_line}:\n{convo}")
        if d and d.note:
            parts.append(f"  Author's note: {d.note}")
        parts.append("")
    if item.instruction:
        parts.append(f"Additional instruction from the author: {item.instruction}")
    return "\n".join(parts)


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
