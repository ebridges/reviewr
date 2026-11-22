"""HTTP API for the frontend. Run with `reviewr serve`."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from reviewr import (
    ai,
    ci,
    cleanup,
    envs,
    fixwork,
    git,
    guidance,
    jira,
    merge,
    secrets,
    submit,
    workspace,
)
from reviewr.agent import agent_prompt
from reviewr.config import GUIDANCE_STEPS, Config, load_config, save_config, settings_path
from reviewr.editor import EditorError, open_in_editor
from reviewr.github import GitHubClient, GitHubError, fetch_pull_request
from reviewr.github.fetch import PR_LISTS, close_pull_request, list_pull_requests, set_draft
from reviewr.llm import ci as ci_ai
from reviewr.llm import conflicts as conflict_ai
from reviewr.llm.client import LLMError
from reviewr.llm.planning import make_plan
from reviewr.llm.replies import draft_replies
from reviewr.llm.triage import suggest, threads_hash
from reviewr.models import PRRef, PRSummary, PullRequest, StaleTrace
from reviewr.state import RESOLVE_BY_DEFAULT, Decision, JiraIssue, ReviewState, ThreadDecision
from reviewr.store import Store

# Configured here as well as in the CLI, because `serve --reload` imports this module
# in a fresh worker process. D7: GitHub cost lines are logged at INFO.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)
app = FastAPI(title="reviewr")

# D4: a refresh within this window returns the cached copy instead of calling GitHub.
MIN_REFRESH_INTERVAL = timedelta(seconds=30)


@lru_cache
def store() -> Store:
    return Store(load_config().data_dir / "reviewr.sqlite3")


@lru_cache
def github() -> GitHubClient:
    return GitHubClient()


def _ref(owner: str, repo: str, number: int) -> PRRef:
    return PRRef(owner=owner, repo=repo, number=number)


def _load_pr(ref: PRRef, refresh: bool = False) -> PullRequest:
    cached = store().get_pr(ref)
    fresh = cached and datetime.now(UTC) - cached.fetched_at < MIN_REFRESH_INTERVAL
    if cached and (not refresh or fresh):
        return cached
    try:
        pr = fetch_pull_request(github(), ref)
    except GitHubError as e:
        raise HTTPException(502, str(e)) from e
    store().put_pr(pr)
    return pr


def _repo_path(ref: PRRef) -> Path:
    try:
        return load_config().repo_path(ref.slug)
    except LookupError as e:
        raise HTTPException(400, str(e)) from e


def _llm(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except LLMError as e:
        raise HTTPException(502, str(e)) from e


def _traces(pr: PullRequest) -> dict[str, StaleTrace]:
    repo = _repo_path(pr.ref)
    return {
        t.id: git.trace_stale(repo, t.path, t.original_commit, t.original_line, pr.head_oid)
        for t in pr.open_threads
        if t.is_outdated
    }


class PRView(BaseModel):
    pr: PullRequest
    state: ReviewState


PR = "/api/prs/{owner}/{repo}/{number}"


# PR list and loading

@app.get("/api/prs")
def get_prs(which: str = "mine") -> list[PRSummary]:
    if which not in PR_LISTS:
        raise HTTPException(400, f"Unknown list {which!r}")
    try:
        return list_pull_requests(github(), which)
    except GitHubError as e:
        raise HTTPException(502, str(e)) from e


class StatusRequest(BaseModel):
    node_id: str
    status: Literal["draft", "ready", "closed"]


@app.post(PR + "/status")
def post_status(owner: str, repo: str, number: int, req: StatusRequest) -> dict:
    """Change a PR's status on GitHub: draft, ready for review (notifies reviewers), or
    closed without merging."""
    try:
        if req.status == "closed":
            return {"state": close_pull_request(github(), req.node_id)}
        is_draft = set_draft(github(), req.node_id, req.status == "draft")
        return {"state": "OPEN", "is_draft": is_draft}
    except GitHubError as e:
        raise HTTPException(502, str(e)) from e


@app.get(PR)
def get_pr(owner: str, repo: str, number: int, refresh: bool = False) -> PRView:
    ref = _ref(owner, repo, number)
    pr = _load_pr(ref, refresh)
    fixwork.recover_interrupted(store(), pr)
    return PRView(pr=pr, state=store().get_state(ref))


# Overview and Triage

@app.post(PR + "/suggest")
def post_suggest(owner: str, repo: str, number: int, force: bool = False) -> ReviewState:
    """One Claude call that suggests a decision for every open thread."""
    ref = _ref(owner, repo, number)
    pr = _load_pr(ref)
    state = store().get_state(ref)
    h = threads_hash(pr)
    if state.suggested_for == h and state.suggestions and not force:
        return state
    suggestions = _llm(suggest, pr, _traces(pr), load_config().llm)

    def save(s: ReviewState) -> None:
        s.suggestions, s.suggested_for = suggestions, h

    return store().update_state(ref, save)


@app.get(PR + "/threads/{thread_id}/trace")
def get_trace(owner: str, repo: str, number: int, thread_id: str) -> StaleTrace:
    ref = _ref(owner, repo, number)
    pr = _load_pr(ref)
    t = pr.thread(thread_id)
    return git.trace_stale(_repo_path(ref), t.path, t.original_commit, t.original_line, pr.head_oid)


class DecideRequest(BaseModel):
    thread_ids: list[str]
    decision: Decision
    note: str | None = None


@app.put(PR + "/decisions")
def put_decisions(owner: str, repo: str, number: int, req: DecideRequest) -> ReviewState:
    ref = _ref(owner, repo, number)

    def save(s: ReviewState) -> None:
        now = datetime.now(UTC)
        for tid in req.thread_ids:
            prev = s.decisions.get(tid)
            note = req.note if req.note is not None else (prev.note if prev else "")
            # Keep a drafted reply only if the decision didn't change.
            reply = prev.reply if prev and prev.decision == req.decision else ""
            s.decisions[tid] = ThreadDecision(
                decision=req.decision, note=note, reply=reply,
                resolve=req.decision in RESOLVE_BY_DEFAULT, decided_at=now,
            )

    return store().update_state(ref, save)


class ClearRequest(BaseModel):
    thread_ids: list[str]


@app.post(PR + "/decisions/clear")
def post_clear_decisions(owner: str, repo: str, number: int, req: ClearRequest) -> ReviewState:
    """Undo decisions for several threads at once (their drafted replies go too)."""
    def clear(s: ReviewState) -> None:
        for tid in req.thread_ids:
            s.decisions.pop(tid, None)

    return store().update_state(_ref(owner, repo, number), clear)


@app.delete(PR + "/decisions/{thread_id}")
def delete_decision(owner: str, repo: str, number: int, thread_id: str) -> ReviewState:
    return store().update_state(_ref(owner, repo, number), lambda s: s.decisions.pop(thread_id, None))


class DecisionPatch(BaseModel):
    note: str | None = None
    reply: str | None = None
    resolve: bool | None = None


@app.patch(PR + "/decisions/{thread_id}")
def patch_decision(owner: str, repo: str, number: int, thread_id: str, req: DecisionPatch) -> ReviewState:
    def save(s: ReviewState) -> None:
        d = s.decisions.get(thread_id)
        if not d:
            raise HTTPException(404, "No decision for this thread yet")
        for field in ("note", "reply", "resolve"):
            if (v := getattr(req, field)) is not None:
                setattr(d, field, v)

    return store().update_state(_ref(owner, repo, number), save)


class DraftRepliesRequest(BaseModel):
    thread_ids: list[str] | None = None  # None: every decided thread without a reply


@app.post(PR + "/replies/draft")
def post_draft_replies(owner: str, repo: str, number: int, req: DraftRepliesRequest) -> ReviewState:
    ref = _ref(owner, repo, number)
    pr = _load_pr(ref)
    state = store().get_state(ref)
    ids = req.thread_ids or [tid for tid, d in state.decisions.items() if not d.reply.strip()]
    drafts = _llm(draft_replies, pr, state, ids, load_config().llm)

    def save(s: ReviewState) -> None:
        for tid, text in drafts.items():
            if tid in s.decisions:
                s.decisions[tid].reply = text

    return store().update_state(ref, save)


# Plan and Fix

@app.post(PR + "/plan")
def post_plan(owner: str, repo: str, number: int) -> ReviewState:
    ref = _ref(owner, repo, number)
    pr = _load_pr(ref)
    state = store().get_state(ref)
    if any(w.status in ("drafting", "committed") for w in state.plan):
        raise HTTPException(409, "Work has started on this plan; it can't be regenerated.")
    items = _llm(make_plan, pr, state, load_config().llm)
    return store().update_state(ref, lambda s: setattr(s, "plan", items))


class ItemPatch(BaseModel):
    mode: str | None = None
    title: str | None = None
    instruction: str | None = None
    prompt: str | None = None  # your edited agent prompt
    reset_prompt: bool = False  # go back to the generated prompt


@app.patch(PR + "/plan/{item_id}")
def patch_item(owner: str, repo: str, number: int, item_id: int, req: ItemPatch) -> ReviewState:
    def save(s: ReviewState) -> None:
        item = s.item(item_id)
        if req.mode in ("agent", "me"):
            item.mode = req.mode
        if req.title:
            item.title = req.title
        if req.instruction is not None:
            item.instruction = req.instruction
        if req.reset_prompt:
            item.prompt = None
        elif req.prompt is not None:
            item.prompt = req.prompt if req.prompt.strip() else None

    return store().update_state(_ref(owner, repo, number), save)


@app.get(PR + "/plan/{item_id}/prompt")
def get_item_prompt(owner: str, repo: str, number: int, item_id: int) -> dict:
    """The prompt the agent will get for this item, and the generated default."""
    ref = _ref(owner, repo, number)
    pr, state = _load_pr(ref), store().get_state(ref)
    item = state.item(item_id)
    default = agent_prompt(pr, state, item)
    return {"prompt": item.prompt or default, "default": default, "edited": item.prompt is not None}


@app.post(PR + "/plan/draft-all")
def post_draft_all(owner: str, repo: str, number: int) -> ReviewState:
    """Start background drafts for every agent item that doesn't have one yet."""
    ref = _ref(owner, repo, number)
    pr = _load_pr(ref)
    cfg = load_config()
    for item in store().get_state(ref).plan:
        if item.mode == "agent" and item.status in ("pending", "failed"):
            fixwork.start_draft(store(), cfg, pr, item.id)
    return store().get_state(ref)


@app.post(PR + "/plan/{item_id}/draft")
def post_draft(owner: str, repo: str, number: int, item_id: int) -> ReviewState:
    ref = _ref(owner, repo, number)
    fixwork.start_draft(store(), load_config(), _load_pr(ref), item_id)
    return store().get_state(ref)


class CommitRequest(BaseModel):
    message: str
    paths: list[str] | None = None  # your own items: the files to include


@app.post(PR + "/plan/{item_id}/accept")
def post_accept(owner: str, repo: str, number: int, item_id: int, req: CommitRequest) -> ReviewState:
    ref = _ref(owner, repo, number)
    try:
        return fixwork.accept_patch(store(), load_config(), _load_pr(ref), item_id, req.message)
    except (ValueError, git.GitError) as e:
        raise HTTPException(409, str(e)) from e


@app.post(PR + "/plan/{item_id}/checks")
def post_run_checks(owner: str, repo: str, number: int, item_id: int) -> ReviewState:
    """Lint and tests for the item's changed files only (never end-to-end suites)."""
    ref = _ref(owner, repo, number)
    try:
        fixwork.start_checks(store(), load_config(), _load_pr(ref), item_id)
    except (ValueError, git.GitError) as e:
        raise HTTPException(409, str(e)) from e
    return store().get_state(ref)


def _env_reader(pr: PullRequest) -> envs.Reader:
    """Lockfiles from reviewr's worktree once work has started (commits may change
    them), otherwise from the PR head commit."""
    work = _work_path(pr)
    return envs.worktree_reader(work) if work else envs.commit_reader(_repo_path(pr.ref), pr.head_oid)


@app.get(PR + "/envs")
def get_envs(owner: str, repo: str, number: int) -> list[envs.EnvStatus]:
    pr = _load_pr(_ref(owner, repo, number))
    if pr.ref.slug not in load_config().repos:
        return []
    return envs.status(load_config(), pr.ref.slug, _env_reader(pr))


_env_jobs = ThreadPoolExecutor(max_workers=2, thread_name_prefix="envs")


@app.post(PR + "/envs/install")
def post_install_envs(owner: str, repo: str, number: int) -> list[envs.EnvStatus]:
    """Install the branch's dependencies in the background (no-op for ready ones)."""
    pr = _load_pr(_ref(owner, repo, number))
    cfg = load_config()
    read = _env_reader(pr)
    for env in cfg.repos[pr.ref.slug].envs:
        _env_jobs.submit(_install_quietly, cfg, pr.ref.slug, env, read)
    time.sleep(0.2)  # let the jobs take their locks so the status shows "installing"
    return envs.status(cfg, pr.ref.slug, read)


def _install_quietly(cfg, slug, env, read) -> None:
    try:
        envs.ensure(cfg, slug, env, read)
    except envs.EnvError:
        log.exception("Installing %s failed", env.name)  # status shows "failed" with the log


@app.get(PR + "/setup")
def get_setup(owner: str, repo: str, number: int) -> dict:
    """Whether the PR's repo needs workspace setup, and the latest setup run."""
    return workspace.view(load_config(), _ref(owner, repo, number))


@app.post(PR + "/setup")
def post_setup(owner: str, repo: str, number: int) -> dict:
    """Clone the repo if needed, check out the PR branch, find the Node and Python
    versions, and install dependencies. Runs in the background; poll GET .../setup."""
    ref = _ref(owner, repo, number)
    cfg = load_config()
    workspace.start(cfg, _load_pr(ref))
    return workspace.view(cfg, ref)


@app.get(PR + "/work")
def get_work(owner: str, repo: str, number: int) -> dict:
    """Where the PR branch is checked out, how it compares with GitHub, what's uncommitted."""
    pr = _load_pr(_ref(owner, repo, number))
    try:
        return fixwork.work_status(load_config(), pr)
    except (LookupError, git.GitError) as e:
        raise HTTPException(400, str(e)) from e


@app.get(PR + "/work/diff")
def get_work_diff(owner: str, repo: str, number: int) -> dict:
    ref = _ref(owner, repo, number)
    cfg = load_config()
    pr = _load_pr(ref)
    return {"diff": fixwork.own_changes(cfg, pr), "worktree": str(fixwork.work_worktree(cfg, pr))}


@app.post(PR + "/plan/{item_id}/check")
def post_check(owner: str, repo: str, number: int, item_id: int) -> ReviewState:
    ref = _ref(owner, repo, number)
    return _llm(fixwork.check_own_changes, store(), load_config(), _load_pr(ref), item_id)


@app.post(PR + "/plan/{item_id}/commit")
def post_commit_own(owner: str, repo: str, number: int, item_id: int, req: CommitRequest) -> ReviewState:
    ref = _ref(owner, repo, number)
    try:
        if not req.paths:
            raise HTTPException(400, "Choose the files to commit.")
        return fixwork.commit_own_changes(store(), load_config(), _load_pr(ref), item_id, req.message, req.paths)
    except git.GitError as e:
        raise HTTPException(409, str(e)) from e


# Jira

class JiraRequest(BaseModel):
    thread_ids: list[str] | None = None  # None: deferred threads not already in an issue


class OwnFixRequest(BaseModel):
    failure: str
    label: str = ""


def _plan_call(fn, *args):
    try:
        return fn(*args)
    except (ValueError, KeyError) as e:
        raise HTTPException(409, str(e)) from e
    except git.GitError as e:
        raise HTTPException(400, str(e)) from e


@app.post(PR + "/plan/{item_id}/fix")
def post_own_fix(owner: str, repo: str, number: int, item_id: int, req: OwnFixRequest) -> ReviewState:
    """Ask the agent to fix failing checks on your own uncommitted changes."""
    ref = _ref(owner, repo, number)
    _repo_path(ref)
    name = "Codex" if ai.current() == "codex" else "Claude"
    return _plan_call(fixwork.start_own_fix, store(), load_config(), _load_pr(ref), item_id, req.failure, name, req.label)


@app.post(PR + "/plan/{item_id}/fix/apply")
def post_own_fix_apply(owner: str, repo: str, number: int, item_id: int) -> ReviewState:
    ref = _ref(owner, repo, number)
    return _plan_call(fixwork.apply_own_fix, store(), load_config(), _load_pr(ref), item_id)


@app.delete(PR + "/plan/{item_id}/fix")
def delete_own_fix(owner: str, repo: str, number: int, item_id: int) -> ReviewState:
    ref = _ref(owner, repo, number)
    return _plan_call(fixwork.discard_own_fix, store(), load_config(), _load_pr(ref), item_id)


@app.post(PR + "/jira")
def post_jira(owner: str, repo: str, number: int, req: JiraRequest) -> ReviewState:
    """Create one Jira issue for deferred threads. This writes to Jira."""
    cfg = load_config()
    if not cfg.jira:
        raise HTTPException(400, "Jira isn't set up. Add it under Settings → Jira.")
    ref = _ref(owner, repo, number)
    pr = _load_pr(ref)
    state = store().get_state(ref)
    tracked = {tid for issue in state.jira_issues for tid in issue.thread_ids}
    ids = req.thread_ids or [tid for tid, d in state.decisions.items() if d.decision == "defer" and tid not in tracked]
    if not ids:
        raise HTTPException(400, "No deferred threads to put in an issue.")
    threads = [pr.thread(tid) for tid in ids]
    titles = {tid: s.title for tid, s in state.suggestions.items()}
    notes = {tid: state.decisions[tid].note for tid in ids if tid in state.decisions}
    try:
        key, url = jira.create_issue(cfg.jira, pr, threads, titles, notes)
    except jira.JiraError as e:
        raise HTTPException(502, str(e)) from e

    def save(s: ReviewState) -> None:
        s.jira_issues.append(JiraIssue(key=key, url=url, thread_ids=ids))
        for tid in ids:
            d = s.decisions.get(tid)
            if d:
                d.note = (d.note + " " if d.note else "") + f"Tracked in {key}."
                d.reply = ""  # redraft so the reply names the issue

    return store().update_state(ref, save)


# Respond

def _work_path(pr: PullRequest) -> Path | None:
    """The checkout of the PR branch if there is one. Never creates it: read-only views
    such as the Respond preview must not change anything in your clone."""
    try:
        return fixwork.existing_work_tree(load_config(), pr)
    except LookupError:
        return None


@app.get(PR + "/submission")
def get_submission(owner: str, repo: str, number: int) -> submit.Submission:
    ref = _ref(owner, repo, number)
    pr = _load_pr(ref)
    return submit.build_submission(pr, store().get_state(ref), _work_path(pr), _github_branch(pr))


def _github_branch(pr: PullRequest) -> str | None:
    """GitHub's copy of the PR branch (origin/<branch>), fetched if it hasn't been in the
    last minute; None if it isn't available locally."""
    repo = load_config().repo_path(pr.ref.slug)
    merge._fetch(repo, pr.head_ref, max_age=60)
    ref = f"origin/{pr.head_ref}"
    ok = git._git(repo, "rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0
    return ref if ok else None


class SummaryRequest(BaseModel):
    summary: str


@app.put(PR + "/summary")
def put_summary(owner: str, repo: str, number: int, req: SummaryRequest) -> ReviewState:
    return store().update_state(_ref(owner, repo, number), lambda s: setattr(s, "summary", req.summary))


class SubmitRequest(BaseModel):
    confirm: bool


@app.post(PR + "/submit")
def post_submit(owner: str, repo: str, number: int, req: SubmitRequest) -> dict:
    if not req.confirm:
        raise HTTPException(400, "Submitting posts to GitHub; send confirm: true.")
    ref = _ref(owner, repo, number)
    pr = _load_pr(ref, refresh=True)
    work = _work_path(pr)
    sub = submit.build_submission(pr, store().get_state(ref), work, _github_branch(pr))
    if sub.missing_replies:
        raise HTTPException(409, f"{len(sub.missing_replies)} decided threads have no reply yet.")
    try:
        done = submit.execute(github(), store(), pr, sub, work)
    except (GitHubError, git.GitError) as e:
        raise HTTPException(502, f"Stopped partway: {e}") from e
    return {"done": done}


# Editor

class OpenRequest(BaseModel):
    path: str | None = None
    line: int | None = None
    editor: str | None = None


@app.post(PR + "/open")
def post_open(owner: str, repo: str, number: int, req: OpenRequest) -> dict:
    """Open your checkout of the PR branch (creating one if you have none) as the
    project, at the file and line. Triage and Fix open the same place."""
    ref = _ref(owner, repo, number)
    pr = _load_pr(ref)
    cfg = load_config()
    try:
        root = fixwork.work_worktree(cfg, pr).resolve()
    except (LookupError, git.GitError) as e:
        raise HTTPException(400, str(e)) from e
    target = (root / req.path).resolve() if req.path else None
    if target is not None and not target.is_relative_to(root):
        raise HTTPException(400, "Path is outside the checkout")
    try:
        argv = open_in_editor(cfg, req.editor, root, target, req.line)
    except EditorError as e:
        raise HTTPException(400, str(e)) from e
    return {"argv": argv, "root": str(root)}


# Storage cleanup

class CleanupView(BaseModel):
    plan: cleanup.CleanupPlan
    remove_mb: int
    keep_mb: int


# Update from main: merge the base branch in and walk through the conflicts (merge.py)

def _merge_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except merge.MergeError as e:
        raise HTTPException(409, str(e)) from e
    except git.GitError as e:
        raise HTTPException(400, str(e)) from e
    except LLMError as e:
        raise HTTPException(502, str(e)) from e


class MergeView(BaseModel):
    session: merge.MergeSession | None
    options: dict[str, list[str]] = {}  # conflict id -> the options it offers, in order


def _merge_view(s: merge.MergeSession | None) -> MergeView:
    if s is None:
        return MergeView(session=None)
    return MergeView(session=s, options={cid: merge.options_for(c) for cid, c in s.conflicts.items()})


def _pr_cfg(owner: str, repo: str, number: int):
    ref = _ref(owner, repo, number)
    _repo_path(ref)
    return load_config(), _load_pr(ref)


@app.get(PR + "/merge/preview")
def get_merge_preview(owner: str, repo: str, number: int, fetch: bool = True) -> dict:
    cfg, pr = _pr_cfg(owner, repo, number)
    return _merge_call(merge.preview, cfg, pr, fetch)


class ConfirmRequest(BaseModel):
    confirm: bool = False


@app.get(PR + "/merge/remote")
def get_merge_remote(owner: str, repo: str, number: int) -> dict:
    """Your checkout of the PR branch compared with the branch on GitHub."""
    cfg, pr = _pr_cfg(owner, repo, number)
    return _merge_call(merge.remote_status, cfg, pr)


@app.post(PR + "/merge/remote/sync")
def post_merge_remote_sync(owner: str, repo: str, number: int, req: ConfirmRequest) -> dict:
    """Bring GitHub's commits on the PR branch into your checkout. Only after you confirm."""
    if not req.confirm:
        raise HTTPException(400, "This changes your checkout, so it needs confirmation.")
    cfg, pr = _pr_cfg(owner, repo, number)
    return _merge_call(merge.sync_with_remote, cfg, pr)


@app.get(PR + "/merge")
def get_merge(owner: str, repo: str, number: int) -> MergeView:
    cfg, pr = _pr_cfg(owner, repo, number)
    return _merge_view(merge.load_current(cfg, pr))


@app.post(PR + "/merge/start")
def post_merge_start(owner: str, repo: str, number: int) -> MergeView:
    cfg, pr = _pr_cfg(owner, repo, number)
    if (old := merge.load(cfg, pr)) and old.status == "committed":
        merge.discard(cfg, pr)
    return _merge_view(_merge_call(merge.start, cfg, pr, conflict_ai.analyze(cfg.llm, pr.title, pr.ref.slug)))


@app.post(PR + "/merge/reanalyze")
def post_merge_reanalyze(owner: str, repo: str, number: int) -> MergeView:
    cfg, pr = _pr_cfg(owner, repo, number)
    return _merge_view(_merge_call(merge.reanalyze, cfg, pr, conflict_ai.analyze(cfg.llm, pr.title, pr.ref.slug)))


class ConflictPatch(BaseModel):
    category: merge.Category | None = None
    choice: merge.Option | None = None
    custom: str | None = None
    resolved: bool | None = None


@app.patch(PR + "/merge/conflicts/{conflict_id}")
def patch_conflict(owner: str, repo: str, number: int, conflict_id: str, req: ConflictPatch) -> MergeView:
    """Recategorize a conflict, choose its resolution, or reopen it."""
    cfg, pr = _pr_cfg(owner, repo, number)

    def change(s: merge.MergeSession) -> None:
        if s.status == "committed":
            raise merge.MergeError("This update is already committed.")
        c = s.conflicts.get(conflict_id)
        if c is None:
            raise merge.MergeError("Unknown conflict.")
        if req.category:
            c.category = req.category
        if req.choice:
            merge.resolve(c, req.choice, req.custom)
        elif req.custom is not None:
            merge._check_text(req.custom)
            c.custom = req.custom
        if req.resolved is False:
            c.resolved, c.choice = False, None
        if req.choice or req.resolved is False:
            s.check_report = None  # the results were for different code
        merge.write_resolved(cfg, pr, s, {c.path})

    return _merge_view(_merge_call(merge.update, cfg, pr, change))


class AcceptRequest(BaseModel):
    conflict_ids: list[str]


@app.post(PR + "/merge/accept")
def post_merge_accept(owner: str, repo: str, number: int, req: AcceptRequest) -> MergeView:
    """Accept the recommended resolution for several conflicts (Triage's "Accept all")."""
    cfg, pr = _pr_cfg(owner, repo, number)

    def change(s: merge.MergeSession) -> None:
        paths = set()
        for cid in req.conflict_ids:
            c = s.conflicts.get(cid)
            if c and c.recommended and not c.resolved:
                merge.resolve(c, c.recommended)
                paths.add(c.path)
        if paths:
            s.check_report = None
        merge.write_resolved(cfg, pr, s, paths)

    return _merge_view(_merge_call(merge.update, cfg, pr, change))


class ProposeRequest(BaseModel):
    instruction: str = ""


@app.post(PR + "/merge/conflicts/{conflict_id}/propose")
def post_conflict_propose(owner: str, repo: str, number: int, conflict_id: str, req: ProposeRequest) -> MergeView:
    """Ask the AI to write code that combines both sides, following your instructions."""
    cfg, pr = _pr_cfg(owner, repo, number)
    s = merge.load(cfg, pr)
    if s is None or conflict_id not in s.conflicts:
        raise HTTPException(404, "Unknown conflict.")
    try:
        result = _merge_call(conflict_ai.propose, cfg.llm, s, s.conflicts[conflict_id], req.instruction, pr.ref.slug)
    except ValueError as e:
        raise HTTPException(502, str(e)) from e

    def change(s: merge.MergeSession) -> None:
        c = s.conflicts[conflict_id]
        c.proposal, c.reason, c.source, c.ai_name = result["proposal"], result["reason"], "ai", result["ai_name"]
        c.recommended = "proposal"
        if c.category == "input" and not c.resolved:
            c.category = "propose"

    return _merge_view(_merge_call(merge.update, cfg, pr, change))


@app.get(PR + "/merge/diff")
def get_merge_diff(owner: str, repo: str, number: int, path: str) -> dict:
    cfg, pr = _pr_cfg(owner, repo, number)
    return {"diff": merge.file_diff(cfg, pr, path)}


@app.post(PR + "/merge/checks")
def post_merge_checks(owner: str, repo: str, number: int) -> MergeView:
    cfg, pr = _pr_cfg(owner, repo, number)
    return _merge_view(_merge_call(merge.start_checks, cfg, pr))


class FixRequest(BaseModel):
    failure: str
    label: str = ""


@app.post(PR + "/merge/fix")
def post_merge_fix(owner: str, repo: str, number: int, req: FixRequest) -> MergeView:
    """Ask the coding agent to fix failing checks or commit hooks in the merge."""
    cfg, pr = _pr_cfg(owner, repo, number)
    name = "Codex" if ai.current() == "codex" else "Claude"
    return _merge_view(_merge_call(merge.start_fix, cfg, pr, req.failure, name, req.label))


@app.post(PR + "/merge/fix/apply")
def post_merge_fix_apply(owner: str, repo: str, number: int) -> MergeView:
    cfg, pr = _pr_cfg(owner, repo, number)
    return _merge_view(_merge_call(merge.apply_fix, cfg, pr))


@app.delete(PR + "/merge/fix")
def delete_merge_fix(owner: str, repo: str, number: int) -> MergeView:
    cfg, pr = _pr_cfg(owner, repo, number)
    return _merge_view(_merge_call(merge.discard_fix, cfg, pr))


class MergeCommitRequest(BaseModel):
    message: str


@app.post(PR + "/merge/commit")
def post_merge_commit(owner: str, repo: str, number: int, req: MergeCommitRequest) -> MergeView:
    cfg, pr = _pr_cfg(owner, repo, number)
    return _merge_view(_merge_call(merge.commit, cfg, pr, req.message.strip() or f"Merge {pr.base_ref} into {pr.head_ref}"))


@app.post(PR + "/merge/push")
def post_merge_push(owner: str, repo: str, number: int, req: ConfirmRequest) -> MergeView:
    """Push the branch to GitHub. Only after the user confirms in the app."""
    if not req.confirm:
        raise HTTPException(400, "Pushing needs confirmation.")
    cfg, pr = _pr_cfg(owner, repo, number)
    return _merge_view(_merge_call(merge.push, cfg, pr))


@app.delete(PR + "/merge")
def delete_merge(owner: str, repo: str, number: int) -> MergeView:
    cfg, pr = _pr_cfg(owner, repo, number)
    merge.discard(cfg, pr)
    return MergeView(session=None)


# Fix CI: failing checks, triaged and fixed (ci.py)

def _ci_call(fn, *args):
    try:
        return fn(*args)
    except ci.CIError as e:
        raise HTTPException(409, str(e)) from e
    except GitHubError as e:
        raise HTTPException(502, str(e)) from e
    except git.GitError as e:
        raise HTTPException(400, str(e)) from e
    except LLMError as e:
        raise HTTPException(502, str(e)) from e


class CIView(BaseModel):
    session: ci.CISession | None


def _ci_analyze(cfg, pr: PullRequest):
    try:
        changed = [f.path for f in git.changed_files(cfg.repo_path(pr.ref.slug), pr.base_oid, pr.head_oid)]
    except git.GitError:
        changed = []
    return ci_ai.analyze(cfg.llm, pr.title, changed, pr.ref.slug)


@app.get(PR + "/ci/status")
def get_ci_status(owner: str, repo: str, number: int, fresh: bool = False) -> dict:
    """The PR's CI state and failing checks; cached for a minute."""
    pr = _load_pr(_ref(owner, repo, number))
    return _ci_call(ci.status, github(), pr, fresh)


@app.get(PR + "/ci")
def get_ci(owner: str, repo: str, number: int) -> CIView:
    cfg, pr = _pr_cfg(owner, repo, number)
    return CIView(session=ci.load_current(cfg, pr))


@app.post(PR + "/ci/start")
def post_ci_start(owner: str, repo: str, number: int) -> CIView:
    cfg, pr = _pr_cfg(owner, repo, number)
    return CIView(session=_ci_call(ci.start, cfg, pr, github(), _ci_analyze(cfg, pr)))


@app.post(PR + "/ci/reanalyze")
def post_ci_reanalyze(owner: str, repo: str, number: int) -> CIView:
    cfg, pr = _pr_cfg(owner, repo, number)
    return CIView(session=_ci_call(ci.reanalyze, cfg, pr, github(), _ci_analyze(cfg, pr)))


class FailurePatch(BaseModel):
    category: Literal["fix", "rerun", "unrelated"] | None = None
    status: Literal["open", "fixed", "dismissed"] | None = None


@app.patch(PR + "/ci/failures/{failure_id}")
def patch_ci_failure(owner: str, repo: str, number: int, failure_id: str, req: FailurePatch) -> CIView:
    """Recategorize a failure, or mark it fixed by you, dismissed, or open again."""
    cfg, pr = _pr_cfg(owner, repo, number)

    def change(s: ci.CISession) -> None:
        f = s.failures.get(failure_id)
        if f is None:
            raise ci.CIError("Unknown failure.")
        if req.category:
            f.category = req.category
        if req.status:
            f.status = req.status

    return CIView(session=_ci_call(ci.update, cfg, pr, change))


@app.post(PR + "/ci/failures/{failure_id}/fix")
def post_ci_fix(owner: str, repo: str, number: int, failure_id: str) -> CIView:
    cfg, pr = _pr_cfg(owner, repo, number)
    name = "Codex" if ai.current() == "codex" else "Claude"
    return CIView(session=_ci_call(ci.start_fix, cfg, pr, failure_id, name))


@app.post(PR + "/ci/failures/{failure_id}/fix/apply")
def post_ci_fix_apply(owner: str, repo: str, number: int, failure_id: str) -> CIView:
    cfg, pr = _pr_cfg(owner, repo, number)
    return CIView(session=_ci_call(ci.apply_fix, cfg, pr, failure_id))


@app.delete(PR + "/ci/failures/{failure_id}/fix")
def delete_ci_fix(owner: str, repo: str, number: int, failure_id: str) -> CIView:
    cfg, pr = _pr_cfg(owner, repo, number)
    return CIView(session=_ci_call(ci.discard_fix, cfg, pr, failure_id))


@app.post(PR + "/ci/failures/{failure_id}/rerun")
def post_ci_rerun(owner: str, repo: str, number: int, failure_id: str, req: ConfirmRequest) -> CIView:
    """Re-run a failed job on GitHub. Only after the user confirms."""
    if not req.confirm:
        raise HTTPException(400, "Re-running a job needs confirmation.")
    cfg, pr = _pr_cfg(owner, repo, number)
    return CIView(session=_ci_call(ci.rerun, cfg, pr, github(), failure_id))


@app.post(PR + "/ci/push")
def post_ci_push(owner: str, repo: str, number: int, req: ConfirmRequest) -> CIView:
    if not req.confirm:
        raise HTTPException(400, "Pushing needs confirmation.")
    cfg, pr = _pr_cfg(owner, repo, number)
    return CIView(session=_ci_call(ci.push, cfg, pr))


@app.delete(PR + "/ci")
def delete_ci(owner: str, repo: str, number: int) -> CIView:
    cfg, pr = _pr_cfg(owner, repo, number)
    ci.discard(cfg, pr)
    return CIView(session=None)


@app.get("/api/cleanup")
def get_cleanup() -> CleanupView:
    """Dry run: what cleanup would remove and keep, and why. Checks each PR reviewr has
    seen against GitHub (about 1 point each)."""
    p = cleanup.plan(load_config(), store(), github())
    return CleanupView(plan=p, remove_mb=p.size_mb, keep_mb=sum(e.size_mb for e in p.envs_keep))


class CleanupRequest(BaseModel):
    confirm: bool


@app.post("/api/cleanup")
def post_cleanup(req: CleanupRequest) -> dict:
    if not req.confirm:
        raise HTTPException(400, "Cleanup deletes files; send confirm: true.")
    cfg = load_config()
    p = cleanup.plan(cfg, store(), github())  # recomputed, so nothing needed since the preview is removed
    return {"done": cleanup.run(cfg, p), "freed_mb": p.size_mb}


# Settings

class ProviderRequest(BaseModel):
    provider: Literal["claude", "codex"]


@app.get("/api/ai")
def get_ai() -> dict:
    """The AI provider in use, and which ones are ready."""
    codex_ok, codex_status = ai.codex_ready()
    return {
        "provider": ai.current(),
        "claude_ready": _claude_status()["ready"],
        "codex_ready": codex_ok,
        "codex_status": codex_status,
    }


@app.put("/api/ai")
def put_ai(req: ProviderRequest) -> dict:
    if req.provider == "codex" and not ai.codex_ready()[0]:
        raise HTTPException(400, "Codex isn't ready. Install the Codex CLI and run `codex login`.")
    ai.set_current(req.provider)
    return get_ai()


@app.get("/api/health")
def get_health() -> dict:
    """Used by the desktop app to know the backend is up."""
    from reviewr import __version__

    return {"ok": True, "version": __version__}


@app.get("/api/config")
def get_config() -> dict:
    cfg = load_config()
    return {
        "editors": list(cfg.editors),
        "default_editor": cfg.default_editor,
        "model": cfg.llm.model,
        "jira_project": cfg.jira.project if cfg.jira else None,
    }


def _ant_profiles() -> list[str]:
    """Profiles saved by `ant auth login` (they're what the SDK signs in with)."""
    creds = Path(os.environ.get("ANTHROPIC_CONFIG_DIR") or Path.home() / ".config" / "anthropic") / "credentials"
    return sorted(p.stem for p in creds.glob("*.json")) if creds.is_dir() else []


def _claude_status() -> dict:
    """How reviewr signs in to the Claude API, and whether that will work."""
    method = load_config().ai.claude_auth
    ant_installed = shutil.which("ant") is not None
    if method == "api_key":
        ready = secrets.has_secret(secrets.ANTHROPIC_API_KEY)
        detail = "API key saved in the Keychain" if ready else "No API key saved"
    elif os.environ.get("ANTHROPIC_API_KEY"):
        ready, detail = True, "ANTHROPIC_API_KEY from your shell (it overrides the ant sign-in)"
    elif os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        ready, detail = True, "ANTHROPIC_AUTH_TOKEN from your shell (it overrides the ant sign-in)"
    elif profiles := _ant_profiles():
        ready, detail = True, f"Signed in with the ant CLI ({', '.join(profiles)})"
    else:
        ready, detail = False, "Not signed in with the ant CLI" if ant_installed else "The ant CLI isn't installed"
    return {"method": method, "ready": ready, "detail": detail, "ant_installed": ant_installed,
            "ant_profiles": _ant_profiles(),
            "login_running": _ant_login.get("proc") is not None and _ant_login["proc"].poll() is None}


_ant_login: dict = {}


@app.post("/api/settings/claude/login")
def post_claude_login() -> dict:
    """Start `ant auth login`, which opens your browser to sign in. Settings polls
    /api/settings until the profile appears."""
    if not shutil.which("ant"):
        raise HTTPException(400, "Install the ant CLI first: brew install anthropics/tap/ant")
    proc = _ant_login.get("proc")
    if proc is None or proc.poll() is not None:
        _ant_login["proc"] = subprocess.Popen(
            ["ant", "auth", "login"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    return _claude_status()


@app.get("/api/settings")
def get_settings() -> dict:
    if os.environ.get("GITHUB_TOKEN"):
        gh_source, gh_login = "GITHUB_TOKEN", None
    else:
        proc = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True, check=False)
        gh_source, gh_login = ("gh CLI", proc.stdout.strip()) if proc.returncode == 0 else (None, None)

    rl = github().last_rate_limit if gh_source else None
    cfg = load_config()
    return {
        "config": cfg.model_dump(mode="json"),
        "secrets": {name: secrets.has_secret(name) for name in secrets.NAMES},
        "claude": _claude_status(),
        "codex": _codex_status(),
        "github": gh_source,
        "github_login": gh_login,
        "rate_limit": rl.__dict__ if rl else None,
        "jira": _jira_status(cfg),
        "guidance": {
            step: {"label": guidance.STEP_LABELS[step], "builtin": guidance.builtin(step)} for step in GUIDANCE_STEPS
        },
        "settings_path": str(settings_path()),
    }


@app.put("/api/settings/config")
def put_settings_config(cfg: Config) -> dict:
    """Save the settings. The whole configuration is sent; it's validated first."""
    save_config(cfg)
    return get_settings()


class SecretRequest(BaseModel):
    value: str


@app.put("/api/settings/secrets/{name}")
def put_secret(name: str, req: SecretRequest) -> dict:
    """Save a secret in the Keychain. It's never sent back to the browser."""
    if name not in secrets.NAMES:
        raise HTTPException(404, "Unknown secret.")
    secrets.set_secret(name, req.value)
    if name == secrets.ANTHROPIC_API_KEY:
        from reviewr.llm.client import _client_for

        _client_for.cache_clear()
    return get_settings()


@app.delete("/api/settings/secrets/{name}")
def delete_secret(name: str) -> dict:
    if name not in secrets.NAMES:
        raise HTTPException(404, "Unknown secret.")
    secrets.delete_secret(name)
    return get_settings()


def _codex_status() -> dict:
    try:
        proc = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=10, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"installed": False, "status": None}
    out = (proc.stdout + proc.stderr).strip().splitlines()
    return {"installed": True, "status": out[0] if out and proc.returncode == 0 else None}


def _jira_status(cfg) -> dict:
    if not cfg.jira:
        return {"configured": False}
    info = {"configured": True, "base_url": cfg.jira.base_url, "project": cfg.jira.project, "issue_type": cfg.jira.issue_type}
    try:
        info["user"] = jira.whoami(cfg.jira)
    except (jira.JiraError, httpx.HTTPError) as e:
        info["error"] = str(e)
    return info


# The built frontend (frontend/dist), served at / so the desktop app and a browser use
# one local address. Mounted last so it never shadows an /api route. In development
# the Vite dev server serves the frontend instead and this folder may not exist.
_UI_DIST = Path(os.environ.get("REVIEWR_UI_DIST", Path(__file__).resolve().parents[2] / "frontend" / "dist"))
if _UI_DIST.is_dir():
    app.mount("/", StaticFiles(directory=_UI_DIST, html=True), name="ui")


@app.middleware("http")
async def no_stale_ui(request, call_next):
    """index.html must never be cached: it names the current build's script files
    (which have content hashes in their names and can be cached), so a reload always
    picks up a new build."""
    response = await call_next(request)
    if not request.url.path.startswith(("/api/", "/assets/")):
        response.headers["Cache-Control"] = "no-cache"
    return response
