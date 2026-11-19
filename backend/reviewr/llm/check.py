"""Check a patch against the threads it is meant to address (the Fix step)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from reviewr.config import LLMConfig
from reviewr.llm.client import structured
from reviewr.llm.render import render_thread
from reviewr.models import PullRequest
from reviewr.state import ThreadCheck

MAX_PATCH_CHARS = 200_000


class _Check(BaseModel):
    thread_id: str
    addressed: bool
    note: str = Field(description="One short sentence: what in the patch addresses it, or what is missing")


class _Result(BaseModel):
    checks: list[_Check]


SYSTEM = """For each review thread, decide whether the patch does what the reviewer
asked. Be strict: a related but different change does not count. Reply with one short
sentence per thread."""


def check_patch(pr: PullRequest, thread_ids: list[str], patch: str, cfg: LLMConfig) -> dict[str, ThreadCheck]:
    if not patch.strip():
        return {tid: ThreadCheck(addressed=False, note="No changes yet.") for tid in thread_ids}
    threads = "\n\n".join(render_thread(pr.thread(tid)) for tid in thread_ids)
    user = f"{threads}\n\n<patch>\n{patch[:MAX_PATCH_CHARS]}\n</patch>"
    result = structured(cfg, SYSTEM, user, _Result, max_tokens=8000)
    wanted = set(thread_ids)
    return {c.thread_id: ThreadCheck(addressed=c.addressed, note=c.note) for c in result.checks if c.thread_id in wanted}
