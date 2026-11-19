"""The Respond step: push commits and post every reply as one review.

`build_submission` is a dry run: it lists exactly what would be sent. `execute` sends
it. The frontend always shows the dry run and asks for confirmation first.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from reviewr import git
from reviewr.github.client import GitHubClient
from reviewr.models import PullRequest
from reviewr.state import DECISION_LABEL, NO_REPLY, ReviewState
from reviewr.store import Store


class PlannedReply(BaseModel):
    thread_id: str
    decision: str
    body: str
    resolve: bool


class Submission(BaseModel):
    branch: str
    commits_to_push: int
    summary: str
    replies: list[PlannedReply]
    missing_replies: list[str]
    rerequest: list[str]


def default_summary(pr: PullRequest, state: ReviewState) -> str:
    lines = []
    committed = [w for w in state.plan if w.commit_sha]
    if committed:
        lines.append(f"Addressed the review in {len(committed)} commit{'s' * (len(committed) != 1)}:")
        lines.append("")
        for w in committed:
            n = len(w.thread_ids)
            lines.append(f"- {w.commit_sha[:7]} {w.title} ({n} thread{'s' * (n != 1)})")
        lines.append("")
    counts: dict[str, int] = {}
    for d in state.decisions.values():
        if d.decision != "fix":
            counts[d.decision] = counts.get(d.decision, 0) + 1
    phrases = {  # (one, many)
        "already_done": ("was already fixed by an earlier commit", "were already fixed by earlier commits"),
        "reply": ("is answered inline", "are answered inline"),
        "push_back": ("explains why the code stays as is", "explain why the code stays as is"),
        "defer": ("is deferred to a follow-up", "are deferred to a follow-up"),
        "no_action": None,
    }
    rest = [f"{n} {phrases[k][n != 1]}" for k, n in counts.items() if phrases.get(k)]
    if rest:
        lines.append("Of the other threads, " + ", ".join(rest) + ".")
    return "\n".join(lines).strip()


def build_submission(pr: PullRequest, state: ReviewState, work: Path | None) -> Submission:
    replies, missing = [], []
    open_ids = {t.id for t in pr.open_threads}
    for tid, d in state.decisions.items():
        if tid not in open_ids:
            continue
        if d.decision in NO_REPLY:
            continue  # nothing to post
        if not d.reply.strip():
            missing.append(tid)
            continue
        replies.append(PlannedReply(thread_id=tid, decision=DECISION_LABEL[d.decision], body=d.reply, resolve=d.resolve))
    return Submission(
        branch=pr.head_ref,
        commits_to_push=git.commits_ahead(work, pr.head_oid) if work and work.exists() else 0,
        summary=state.summary if state.summary is not None else default_summary(pr, state),
        replies=replies,
        missing_replies=missing,
        rerequest=[r.login for r in pr.reviewers if r.state == "CHANGES_REQUESTED" and r.user_id],
    )


def execute(gh: GitHubClient, store: Store, pr: PullRequest, sub: Submission, work: Path | None) -> list[str]:
    done: list[str] = []
    if sub.commits_to_push and work:
        git.push(work, sub.branch)
        done.append(f"Pushed {sub.commits_to_push} commits to {sub.branch}")

    review_id = gh.mutate("start_review", prId=pr.node_id, body=sub.summary)[
        "addPullRequestReview"]["pullRequestReview"]["id"]
    for r in sub.replies:
        gh.mutate("reply_to_thread", reviewId=review_id, threadId=r.thread_id, body=r.body)
    url = gh.mutate("submit_review", reviewId=review_id)["submitPullRequestReview"]["pullRequestReview"]["url"]
    done.append(f"Posted {len(sub.replies)} replies in one review: {url}")

    resolved = 0
    for r in sub.replies:
        if r.resolve:
            gh.mutate("resolve_thread", threadId=r.thread_id)
            resolved += 1
    done.append(f"Resolved {resolved} threads")

    ids = [x.user_id for x in pr.reviewers if x.login in sub.rerequest and x.user_id]
    if ids:
        gh.mutate("request_reviews", prId=pr.node_id, userIds=ids)
        done.append(f"Re-requested review from {', '.join(sub.rerequest)}")

    store.update_state(pr.ref, lambda s: setattr(s, "submitted_at", datetime.now(UTC)))
    return done
