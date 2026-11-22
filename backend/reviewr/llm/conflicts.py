"""AI triage of merge conflicts, and proposals for code that combines both sides."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel, Field

from reviewr import ai
from reviewr.config import LLMConfig
from reviewr.llm.client import structured
from reviewr.merge import Category, Conflict, MergeSession

BATCH = 20
MAX_SIDE_LINES = 200

Recommended = Literal["ours", "theirs", "ours_then_theirs", "theirs_then_ours", "proposal"]


class _Item(BaseModel):
    conflict_id: str
    title: str = Field(description="Under 10 words: what the two sides disagree about")
    category: Category
    recommended: Recommended
    proposal: str | None = Field(description="Replacement text for the conflicted lines only, or null")
    reason: str = Field(description="One or two sentences: what each side changed, and why the recommendation")


class _Result(BaseModel):
    conflicts: list[_Item]


SYSTEM = """You help the author of a pull request bring their branch up to date with the
base branch (main). Git could not merge some regions on its own. For each conflict you get
the common ancestor (<base>), the PR's version (<pr>), main's version (<main>), the lines
around it, and the commits on each side that touched the file.

Put each conflict in one category:
- auto: one resolution is clearly right and needs no judgement. For example, both sides
  added different imports, list entries or functions and keeping both is correct, or one
  side's change is already contained in the other.
- input: both sides changed the same code differently, and one of the plain options
  (keep the PR's version, take main's, keep both) is probably right, but the author
  should confirm because it depends on what they intended.
- propose: none of the plain options is correct, and the code has to be rewritten to keep
  both changes. For example main renamed a function or changed a signature that the PR's
  new code uses, or both sides edited the same expression for different reasons.

recommended is one of ours (the PR's version), theirs (main's version), ours_then_theirs,
theirs_then_ours, or proposal. Use proposal only when you wrote one.

proposal is the replacement for the conflicted lines only: not the surrounding lines,
no conflict markers, using the file's indentation, ending with a newline. It is
required for propose. For auto it is allowed when keeping both sides needs a small edit
(such as merging two import lists into one sorted list). Otherwise use null.

Be specific in reason: name what the PR changed and what main changed. Every conflict id
in the input must appear exactly once in the output."""


def _clip(text: str | None) -> str:
    if text is None:
        return "(file deleted)"
    lines = text.splitlines(keepends=True)
    if len(lines) <= MAX_SIDE_LINES:
        return text
    return "".join(lines[:MAX_SIDE_LINES]) + f"… ({len(lines) - MAX_SIDE_LINES} more lines)\n"


def render(s: MergeSession, c: Conflict) -> str:
    f = next(f for f in s.files if f.path == c.path)
    pr_log = "\n".join(f"{x.sha} {x.subject}" for x in f.pr_commits) or "(none)"
    main_log = "\n".join(f"{x.sha} {x.subject}" for x in f.main_commits) or "(none)"
    return (
        f'<conflict id="{c.id}" path="{c.path}" line="{c.start_line or "?"}">\n'
        f"<pr_commits>\n{pr_log}\n</pr_commits>\n<main_commits>\n{main_log}\n</main_commits>\n"
        f"<before>\n{c.before}</before>\n"
        f"<pr>\n{_clip(c.ours)}</pr>\n<base>\n{_clip(c.base)}</base>\n<main>\n{_clip(c.theirs)}</main>\n"
        f"<after>\n{c.after}</after>\n</conflict>"
    )


def _clean(proposal: str | None) -> str | None:
    if proposal is None or re.search(r"^(<{7}|={7}|>{7})", proposal, re.MULTILINE):
        return None
    return proposal if proposal.endswith("\n") or not proposal else proposal + "\n"


def _batch(s: MergeSession, conflicts: list[Conflict], cfg: LLMConfig, pr_title: str, slug: str | None) -> dict[str, dict]:
    user = f"PR: {pr_title}\nBranch: {s.head_ref}, being updated from {s.base_ref}\n\n" + "\n\n".join(render(s, c) for c in conflicts)
    result = structured(cfg, SYSTEM, user, _Result, step="conflicts", slug=slug)
    known = {c.id for c in conflicts}
    out: dict[str, dict] = {}
    for item in result.conflicts:
        if item.conflict_id not in known:
            continue
        proposal = _clean(item.proposal)
        recommended = item.recommended if item.recommended != "proposal" or proposal else "ours"
        category = item.category if item.category != "propose" or proposal else "input"
        out[item.conflict_id] = {
            "title": item.title, "category": category, "recommended": recommended,
            "proposal": proposal, "reason": item.reason, "source": "ai", "ai_name": _name(),
        }
    return out


def analyze(cfg: LLMConfig, pr_title: str, slug: str | None = None):
    """A function merge.start can call with (session, conflicts), in batches."""

    def run(s: MergeSession, conflicts: list[Conflict]) -> dict[str, dict]:
        batches = [conflicts[i : i + BATCH] for i in range(0, len(conflicts), BATCH)]
        out: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            for part in pool.map(lambda b: _batch(s, b, cfg, pr_title, slug), batches):
                out.update(part)
        return out

    return run


class _Proposal(BaseModel):
    proposal: str = Field(description="Replacement text for the conflicted lines only")
    reason: str = Field(description="One or two sentences on how it combines the two sides")


PROPOSE_SYSTEM = """You resolve one merge conflict for the author of a pull request that is
being updated from main. Write code that replaces the conflicted lines only (not the
surrounding <before>/<after> lines), keeps what each side was trying to do, and follows
the author's instructions if any. No conflict markers. Use the file's indentation and end
with a newline."""


def propose(cfg: LLMConfig, s: MergeSession, c: Conflict, instruction: str, slug: str | None = None) -> dict:
    user = render(s, c)
    if instruction.strip():
        user += f"\n\n<author_instructions>\n{instruction.strip()}\n</author_instructions>"
    r = structured(cfg, PROPOSE_SYSTEM, user, _Proposal, max_tokens=16000, step="conflicts", slug=slug)
    proposal = _clean(r.proposal)
    if proposal is None:
        raise ValueError("The proposal still contained conflict markers.")
    return {"proposal": proposal, "reason": r.reason, "source": "ai", "ai_name": _name()}


def _name() -> str:
    return "Codex" if ai.current() == "codex" else "Claude"
