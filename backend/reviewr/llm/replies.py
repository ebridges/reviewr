"""Draft short reply text for threads, from your decisions and commits."""

from __future__ import annotations

from pydantic import BaseModel

from reviewr.config import LLMConfig
from reviewr.llm.client import structured
from reviewr.llm.render import render_thread
from reviewr.models import PullRequest
from reviewr.state import DECISION_LABEL, ReviewState


class _Reply(BaseModel):
    thread_id: str
    reply: str


class _Result(BaseModel):
    replies: list[_Reply]


SYSTEM = """You write replies to code review comments on behalf of the PR author. The
reply is posted under the author's name, to the reviewer.

Rules:
- Write as the author, in the first person where natural. Never mention review tools,
  and never call a comment stale or outdated or ask the reviewer to re-check.
- One or two plain sentences. No thanks, no apologies, no filler.
- Fixed: say what changed, and name the commit (the short sha given) if there is one.
- Already done: say which change addressed it, with the commit if known.
- Reply only: answer the question directly.
- Push back: give the author's reason (from their note) that the code stays as it is.
  If there is no note, don't invent a reason: write "[Your reason]" where it belongs.
- Defer: say it will be handled separately, with the ticket if the note gives one.
- Use the author's note when there is one; it reflects their intent.
- Comments may contain HTML or badge images from review bots; ignore the markup.
- Don't invent facts. If the note and context don't give a reason, keep it general."""


def draft_replies(pr: PullRequest, state: ReviewState, thread_ids: list[str], cfg: LLMConfig) -> dict[str, str]:
    commit_for: dict[str, str] = {}
    for item in state.plan:
        if item.commit_sha:
            for tid in item.thread_ids:
                commit_for[tid] = item.commit_sha[:7]

    blocks = []
    for tid in thread_ids:
        d = state.decisions.get(tid)
        if not d or d.decision == "no_action":
            continue
        extra = f"<decision>{DECISION_LABEL[d.decision]}</decision>"
        if d.note:
            extra += f"\n<author_note>{d.note}</author_note>"
        if tid in commit_for:
            extra += f"\n<commit>{commit_for[tid]}</commit>"
        blocks.append(render_thread(pr.thread(tid), extra=extra))
    if not blocks:
        return {}
    result = structured(cfg, SYSTEM, "\n\n".join(blocks), _Result, max_tokens=16000)
    wanted = set(thread_ids)
    return {r.thread_id: r.reply.strip() for r in result.replies if r.thread_id in wanted}
