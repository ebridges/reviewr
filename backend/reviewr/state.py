"""Per-PR review state: Claude's suggestions, your decisions, the plan, and replies.

Stored as one JSON document per PR in SQLite (see store.py). It is small (one entry
per thread and per work item), and keeping it in one document makes it easy to
inspect and to change shape while the workflow is still settling.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from reviewr.checks import CheckReport

Decision = Literal["fix", "reply", "push_back", "already_done", "defer", "no_action"]
StaleVerdict = Literal["addressed", "not_addressed", "unclear"]

DECISION_LABEL: dict[str, str] = {
    "fix": "Fix",
    "reply": "Reply only",
    "push_back": "Push back",
    "already_done": "Already done",
    "defer": "Defer",
    "no_action": "No action",
}

# Nothing is posted for these: no reply, and the thread is left as it is.
NO_REPLY: set[str] = {"no_action"}

# Threads with these decisions are resolved when the reply posts; the others stay
# open for the reviewer to resolve.
RESOLVE_BY_DEFAULT: set[str] = {"fix", "already_done"}


class Suggestion(BaseModel):
    thread_id: str
    title: str
    decision: Decision
    reason: str
    similar: list[str] = Field(default_factory=list)
    stale_verdict: StaleVerdict | None = None


class ThreadDecision(BaseModel):
    decision: Decision
    note: str = ""
    reply: str = ""
    resolve: bool = False
    decided_at: datetime


class JiraIssue(BaseModel):
    key: str
    url: str
    thread_ids: list[str]


class ThreadCheck(BaseModel):
    addressed: bool
    note: str


class ProposedFix(BaseModel):
    """A change the coding agent proposes to make failing checks pass on your own
    uncommitted edits. Nothing touches your checkout until you apply it."""
    status: Literal["running", "ready", "failed"]
    failure: str
    label: str = ""  # what failed, e.g. "Backend tests (pytest)" or "commit hooks"
    ai_name: str
    diff: str = ""
    summary: str = ""
    error: str | None = None


class WorkItem(BaseModel):
    id: int
    title: str
    thread_ids: list[str]
    mode: Literal["agent", "me"] = "agent"
    status: Literal["pending", "drafting", "ready", "failed", "committed"] = "pending"
    patch: str = ""
    checks: dict[str, ThreadCheck] = Field(default_factory=dict)
    commit_sha: str | None = None
    error: str | None = None
    agent_log: str = ""
    instruction: str = ""
    prompt: str | None = None  # your edited agent prompt; None means the generated default
    check_report: CheckReport | None = None
    checks_running: bool = False
    proposed_fix: ProposedFix | None = None


class ReviewState(BaseModel):
    suggestions: dict[str, Suggestion] = Field(default_factory=dict)
    suggested_for: str | None = Field(None, description="Hash of the threads suggestions cover")
    decisions: dict[str, ThreadDecision] = Field(default_factory=dict)
    plan: list[WorkItem] = Field(default_factory=list)
    jira_issues: list[JiraIssue] = Field(default_factory=list)
    summary: str | None = None
    submitted_at: datetime | None = None

    def item(self, item_id: int) -> WorkItem:
        for w in self.plan:
            if w.id == item_id:
                return w
        raise KeyError(item_id)

    def fix_thread_ids(self) -> list[str]:
        return [tid for tid, d in self.decisions.items() if d.decision == "fix"]
