"""Suggest a decision for every open thread with one Claude call (the Overview step)."""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field

from reviewr.config import LLMConfig
from reviewr.llm.client import structured
from reviewr.llm.render import render_thread
from reviewr.models import PullRequest, StaleTrace
from reviewr.state import Decision, StaleVerdict, Suggestion


class _Item(BaseModel):
    thread_id: str
    title: str = Field(description="Under 10 words: what the reviewer is asking for")
    decision: Decision
    reason: str = Field(description="One sentence explaining the suggestion")
    similar_thread_ids: list[str] = Field(
        description="Other threads asking for the same change, which one commit could address"
    )
    stale_verdict: StaleVerdict | None = Field(
        description="Only for stale threads: does the current code do what the reviewer asked?"
    )


class _Result(BaseModel):
    threads: list[_Item]


SYSTEM = """You help the author of a large pull request work through review comments.

For every thread, suggest one decision:
- fix: the reviewer asks for a code change that belongs in this PR.
- reply: a question or remark that needs an answer, not a code change.
- push_back: the request conflicts with another comment, with the PR's goal, or is
  likely wrong. Only suggest this with a concrete reason.
- already_done: only for stale threads whose current code clearly does what the
  reviewer asked.
- defer: reasonable, but clearly larger than this PR.
- no_action: nothing to do and nothing to say. Typically a note the PR author left for
  reviewers (a thread started by the author, given in <pr_author>), or a comment that
  only acknowledges something. Use reply instead if someone else asked a question in it,
  and fix if the author's note reads like a to-do.

Stale threads are ones where the code under the comment changed after it was written.
That does not mean the comment was addressed: a merge, rename or reformat also makes a
thread stale. For each stale thread, compare <code_when_commented> with <current_code>
and set stale_verdict. If you can't tell, use "unclear" and suggest fix or reply,
never already_done.

List similar threads only when one commit could address them together. Every thread id
in the input must appear exactly once in the output."""


def threads_hash(pr: PullRequest) -> str:
    ids = sorted(f"{t.id}:{len(t.comments)}" for t in pr.open_threads)
    return hashlib.sha256("\n".join(ids).encode()).hexdigest()[:16]


def suggest(pr: PullRequest, traces: dict[str, StaleTrace], cfg: LLMConfig) -> dict[str, Suggestion]:
    rendered = "\n\n".join(render_thread(t, traces.get(t.id)) for t in pr.open_threads)
    user = f"PR: {pr.title}\n<pr_author>{pr.author}</pr_author>\n\n{rendered}"
    result = structured(cfg, SYSTEM, user, _Result, step="suggest", slug=pr.ref.slug)
    known = {t.id for t in pr.open_threads}
    out: dict[str, Suggestion] = {}
    for item in result.threads:
        if item.thread_id not in known:
            continue
        out[item.thread_id] = Suggestion(
            thread_id=item.thread_id,
            title=item.title,
            decision=item.decision,
            reason=item.reason,
            similar=[s for s in item.similar_thread_ids if s in known and s != item.thread_id],
            stale_verdict=item.stale_verdict,
        )
    return out
