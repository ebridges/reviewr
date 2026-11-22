"""Run a coding agent in a draft worktree to make a work item's changes.

Claude: an agent loop over the Messages API with file tools only (agent_tools.py: the
text editor tool plus find and search, all confined to the worktree; no shell).
Codex: `codex exec` in its workspace-write sandbox, which confines writes to the
worktree and has no network access; unlike the Claude setup it can run commands there.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path

import anthropic

from reviewr import ai
from reviewr.agent_tools import TOOLS, ToolError, Workspace
from reviewr.config import load_config
from reviewr.guidance import with_guidance
from reviewr.models import PullRequest
from reviewr.state import ReviewState, WorkItem

log = logging.getLogger(__name__)

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


SYSTEM = """You are a careful software engineer making changes in a git repository to address
code review comments. The repository is checked out at the PR's branch; paths are relative to
its root.

You have file tools only: view and edit files with the text editor tool, list files with
find_files, and search their contents with search. You can't run commands, tests or builds.
Look at the relevant code before you change it. When you're done, stop calling tools and reply
with one short paragraph saying what you changed and anything you weren't sure about."""

MAX_TURNS = 80


def _run_claude(worktree: Path, prompt: str, model: str, slug: str | None) -> str:
    from reviewr.llm.client import LLMError, anthropic_client

    cfg = load_config()
    try:
        client = anthropic_client()
    except LLMError as e:
        raise AgentError(str(e)) from e
    ws = Workspace(worktree)
    system = with_guidance(SYSTEM, "agent", slug, root=worktree)
    messages: list = [{"role": "user", "content": prompt}]
    notes: list[str] = []
    for _turn in range(MAX_TURNS):
        try:
            with client.beta.messages.stream(
                model=model,
                max_tokens=64000,
                system=system,
                tools=TOOLS,
                messages=messages,
                cache_control={"type": "ephemeral"},
                output_config={"effort": cfg.llm.effort},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            ) as stream:
                response = stream.get_final_message()
        except anthropic.APIError as e:
            raise AgentError(f"The Claude API returned an error: {e}") from e
        # Keep the whole content (thinking and tool calls included), as the API requires.
        messages.append({"role": "assistant", "content": response.content})
        notes.extend(b.text for b in response.content if b.type == "text" and b.text.strip())
        if response.stop_reason == "refusal":
            raise AgentError("Claude declined to make this change.")
        if response.stop_reason == "max_tokens":
            raise AgentError("Claude's reply was cut off (max_tokens). Try a smaller work item.")
        calls = [b for b in response.content if b.type == "tool_use"]
        if not calls:
            return "\n\n".join(notes)
        results = []
        for call in calls:
            try:
                out, is_error = ws.run(call.name, call.input if isinstance(call.input, dict) else json.loads(call.input)), False
            except ToolError as e:
                out, is_error = str(e), True
            except (OSError, ValueError, KeyError, TypeError) as e:
                out, is_error = f"The tool failed: {e}", True
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": out, "is_error": is_error})
        messages.append({"role": "user", "content": results})  # all results in one message
    raise AgentError(f"The agent didn't finish within {MAX_TURNS} turns. Try a smaller work item.")


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


def run_agent(worktree: Path, prompt: str, model: str, slug: str | None = None) -> str:
    """Draft changes in `worktree` with the current provider. `model` is the Claude model;
    `slug` picks the repo's guidance."""
    if ai.current() == "codex":
        return _run_codex(worktree, with_guidance(prompt, "agent", slug, root=worktree))
    return _run_claude(worktree, prompt, model, slug)
