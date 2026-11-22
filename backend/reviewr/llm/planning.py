"""Group the threads marked Fix into work items, one commit each (the Plan step)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from reviewr.config import LLMConfig
from reviewr.llm.client import structured
from reviewr.llm.render import render_thread
from reviewr.models import PullRequest
from reviewr.state import ReviewState, WorkItem


class _Item(BaseModel):
    title: str = Field(description="Imperative, under 10 words, usable as a commit subject")
    thread_ids: list[str]


class _Result(BaseModel):
    items: list[_Item] = Field(description="In the order they should be committed")


SYSTEM = """Group code review threads into work items for the PR author. Each work item
becomes one commit, so put threads together when one coherent change addresses them
all (the same refactor in several places, the same kind of cleanup). Keep unrelated
changes apart. Order items so that ones others depend on come first. Every thread id
must appear in exactly one item."""


def make_plan(pr: PullRequest, state: ReviewState, cfg: LLMConfig) -> list[WorkItem]:
    fix_ids = state.fix_thread_ids()
    if not fix_ids:
        return []
    blocks = []
    for tid in fix_ids:
        d = state.decisions[tid]
        extra = f"<author_note>{d.note}</author_note>" if d.note else ""
        blocks.append(render_thread(pr.thread(tid), extra=extra))
    result = structured(cfg, SYSTEM, "\n\n".join(blocks), _Result, max_tokens=16000, step="plan", slug=pr.ref.slug)

    remaining = set(fix_ids)
    items: list[WorkItem] = []
    for it in result.items:
        ids = [t for t in it.thread_ids if t in remaining]
        remaining -= set(ids)
        if ids:
            items.append(WorkItem(id=len(items) + 1, title=it.title, thread_ids=ids))
    # Anything Claude left out still needs a home.
    for tid in fix_ids:
        if tid in remaining:
            title = state.suggestions.get(tid)
            items.append(
                WorkItem(id=len(items) + 1, title=title.title if title else "Address review comment", thread_ids=[tid])
            )
    return items
