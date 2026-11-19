"""Data shapes shared by the GitHub client, the cache, and the HTTP API."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class PRRef(BaseModel):
    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def key(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"

    @classmethod
    def parse(cls, text: str) -> PRRef:
        """Accept a PR URL or `owner/repo#123`."""
        m = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", text) or re.fullmatch(
            r"\s*([^/\s]+)/([^#\s]+)#(\d+)\s*", text
        )
        if not m:
            raise ValueError(f"Not a pull request URL or owner/repo#N reference: {text!r}")
        return cls(owner=m.group(1), repo=m.group(2), number=int(m.group(3)))


class Comment(BaseModel):
    id: str
    database_id: int | None = None
    author: str
    body: str
    created_at: datetime
    url: str


class Thread(BaseModel):
    """A review thread: a comment anchored to a line, plus its replies."""

    id: str
    path: str
    line: int | None = Field(None, description="Line at the PR head; None when stale")
    original_line: int | None = None
    start_line: int | None = None
    diff_side: Literal["LEFT", "RIGHT"] = "RIGHT"
    is_resolved: bool
    is_outdated: bool = Field(description="GitHub's 'outdated'; reviewr calls this stale")
    original_commit: str | None = None
    diff_hunk: str = ""
    comments: list[Comment]

    @property
    def url(self) -> str:
        return self.comments[0].url if self.comments else ""

    @property
    def reviewer(self) -> str:
        return self.comments[0].author if self.comments else "ghost"


class Review(BaseModel):
    id: str
    author: str
    state: str
    body: str
    submitted_at: datetime | None
    url: str


class ReviewerStatus(BaseModel):
    login: str
    user_id: str | None
    state: str


class PullRequest(BaseModel):
    ref: PRRef
    node_id: str
    title: str
    url: str
    author: str
    base_ref: str
    head_ref: str
    base_oid: str
    head_oid: str
    changed_files: int = 0
    threads: list[Thread]
    reviews: list[Review] = Field(description="Only reviews with a non-empty body")
    reviewers: list[ReviewerStatus] = Field(default_factory=list)
    issue_comments: list[Comment]
    fetched_at: datetime

    def thread(self, thread_id: str) -> Thread:
        for t in self.threads:
            if t.id == thread_id:
                return t
        raise KeyError(thread_id)

    @property
    def open_threads(self) -> list[Thread]:
        return [t for t in self.threads if not t.is_resolved]


class PRSummary(BaseModel):
    """One row of the PR list."""

    ref: PRRef
    node_id: str = ""
    title: str
    url: str
    is_draft: bool
    updated_at: datetime
    head_ref: str
    changed_files: int
    review_decision: str | None
    open_threads: int
    ci: str | None


class ChangedFile(BaseModel):
    path: str
    old_path: str | None = None
    status: str
    additions: int | None
    deletions: int | None


class TraceCommit(BaseModel):
    sha: str
    subject: str


class StaleTrace(BaseModel):
    """Where a stale comment originally pointed and what happened to those lines."""

    status: Literal["unchanged", "moved", "edited", "deleted", "unknown"]
    original_commit: str | None
    original_path: str
    original_line: int | None
    current_path: str | None
    current_line: int | None
    commits: list[TraceCommit]
    original_snippet: str
    current_snippet: str
    snippet_start_original: int
    snippet_start_current: int
