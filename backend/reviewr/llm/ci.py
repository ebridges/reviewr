"""AI triage of failing CI checks: fix in this PR, re-run, or not this PR's problem."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from reviewr import ai
from reviewr.ci import CIFailure, CISession
from reviewr.config import LLMConfig
from reviewr.llm.client import structured


class _Item(BaseModel):
    failure_id: str
    title: str = Field(description="Under 10 words: what is failing")
    category: Literal["fix", "rerun", "unrelated"]
    reason: str = Field(description="Two sentences at most: the cause, and what to do about it")
    files: list[str] = Field(description="Files in the repository that most likely need to change; empty if none")


class _Result(BaseModel):
    failures: list[_Item]


SYSTEM = """You help the author of a pull request deal with its failing CI checks. For each
failure you get the check's name, GitHub's annotations, and the log of the step that
failed. You also get the files the PR changes.

Put each failure in one category:
- fix: the PR's changes caused it, or it needs a change on this branch (a type error, a
  failing test, lint, a generated file that's out of date). Name the files to change.
- rerun: flaky or infrastructure: timeouts, cancelled or time-limit-exceeded jobs,
  network or registry errors, runner problems, Docker build failures unrelated to the
  PR's files. Re-running the job is the likely fix.
- unrelated: the failure has nothing to do with this PR and re-running won't help, for
  example the base branch is broken or a required secret is missing.

Be concrete in reason: quote the error (file, symbol, test name) and say what to change or
which command to run. Every failure id in the input must appear exactly once."""


def _render(f: CIFailure) -> str:
    notes = "\n".join(f"  {a.level} {a.path}{f':{a.line}' if a.line else ''}: {a.message[:400]}" for a in f.annotations) or "  (none)"
    return (
        f'<failure id="{f.id}" check="{f.name}" workflow="{f.workflow or ""}" conclusion="{f.conclusion}">\n'
        f"<annotations>\n{notes}\n</annotations>\n<log>\n{f.excerpt[-8000:]}\n</log>\n</failure>"
    )


def analyze(cfg: LLMConfig, pr_title: str, changed_files: list[str], slug: str | None = None):
    """A function ci.start can call with (session, failures)."""

    def run(s: CISession, failures: list[CIFailure]) -> dict[str, dict]:
        if not failures:
            return {}
        files = "\n".join(changed_files[:300]) + (f"\n… and {len(changed_files) - 300} more" if len(changed_files) > 300 else "")
        user = f"PR: {pr_title}\n\n<changed_files>\n{files}\n</changed_files>\n\n" + "\n\n".join(_render(f) for f in failures)
        result = structured(cfg, SYSTEM, user, _Result, step="ci", slug=slug)
        name = "Codex" if ai.current() == "codex" else "Claude"
        known = {f.id for f in failures}
        return {
            r.failure_id: {"title": r.title, "category": r.category, "reason": r.reason, "files": r.files, "ai_name": name}
            for r in result.failures if r.failure_id in known
        }

    return run
