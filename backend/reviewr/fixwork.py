"""The Fix step: drafting work items, checking them, and committing them.

The work happens in your checkout of the PR's branch, so your edits and reviewr's are
the same thing:
- If one of your worktrees (or your clone) has the PR branch checked out, that's it.
  Failing that, a worktree of yours detached exactly at the PR head commit.
- Otherwise reviewr creates one, on the real PR branch, at
  <data_dir>/worktrees/<owner>__<repo>__<number>/work, and Triage and Fix open it.

Accepted patches and your own changes are committed there, one commit per work item.
reviewr never stages your other changes: diffs are read without touching the index,
patch commits include only the patch's files, and lint and tests run in a throwaway
worktree (drafts/...) with a copy of the changes. Nothing is pushed until Respond.
"""

from __future__ import annotations

import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from reviewr import envs, git
from reviewr.agent import prompt_for, run_agent
from reviewr.checks import CheckReport, CheckRun, run_checks
from reviewr.config import Config
from reviewr.llm.check import check_patch
from reviewr.models import PullRequest
from reviewr.state import ReviewState
from reviewr.store import Store

log = logging.getLogger(__name__)

# Two jobs at a time keeps API usage, CPU (tests) and disk churn reasonable.
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fixwork")
_running: set[tuple[str, int]] = set()  # (PR key, item id) with a job in this process


def _submit(pr: PullRequest, item_id: int, fn, *args) -> None:
    """Run a job for a work item in the background, remembering that it's running here."""
    key = (pr.ref.key, item_id)
    _running.add(key)

    def run() -> None:
        try:
            fn(*args)
        finally:
            _running.discard(key)

    _executor.submit(run)


def recover_interrupted(store: Store, pr: PullRequest) -> None:
    """Drafts and checks whose job isn't running in this process were cut
    off by a restart. Mark them so they can be started again instead of waiting forever."""
    state = store.get_state(pr.ref)
    stuck = [w.id for w in state.plan if (pr.ref.key, w.id) not in _running and (
        w.status == "drafting" or w.checks_running)]
    if not stuck:
        return

    def mark(s: ReviewState) -> None:
        for w in s.plan:
            if w.id not in stuck or (pr.ref.key, w.id) in _running:
                continue
            if w.status == "drafting":
                w.status, w.error = "failed", "Interrupted because reviewr restarted. Draft it again."
            w.checks_running = False

    store.update_state(pr.ref, mark)


def _root(cfg: Config, pr: PullRequest) -> Path:
    return cfg.data_dir / "worktrees" / f"{pr.ref.owner}__{pr.ref.repo}__{pr.ref.number}"


def work_root(cfg: Config, pr: PullRequest) -> Path:
    """Where reviewr creates a worktree for the PR branch if you don't have one."""
    return _root(cfg, pr) / "work"


def _yours(cfg: Config, pr: PullRequest) -> Path | None:
    repo = cfg.repo_path(pr.ref.slug)
    # reviewr's drafts/ and merge/ worktrees are also detached at the head; they aren't yours.
    scratch = cfg.data_dir / "worktrees"
    return git.worktree_for_branch(repo, pr.head_ref) or git.worktree_detached_at(repo, pr.head_oid, ignore=scratch)


def existing_work_tree(cfg: Config, pr: PullRequest) -> Path | None:
    """The checkout of the PR branch, if there is one yet. Creates nothing."""
    return _yours(cfg, pr) or (work_root(cfg, pr) if work_root(cfg, pr).exists() else None)


def work_worktree(cfg: Config, pr: PullRequest) -> Path:
    """Your checkout of the PR branch; created on first use if you don't have one."""
    repo = cfg.repo_path(pr.ref.slug)
    git.ensure_commits(repo, pr.head_oid)
    return existing_work_tree(cfg, pr) or git.ensure_branch_worktree(repo, work_root(cfg, pr), pr.head_ref, pr.head_oid)


HOOK_TIMEOUT_S = 900


def commit_with_hooks(cfg: Config, slug: str, worktree: Path, message: str, paths: list[str] | None = None) -> tuple[str, list[str]]:
    """Commit, running the repository's commit hooks the way they run when you commit by
    hand: with the branch's dependencies linked in (so hooks like ESLint find
    node_modules) and the repo's PATH. With `paths`, only those files are committed.

    If the hooks fail only because formatters changed files, those changes are staged
    and the commit is tried once more, as you would by hand. Returns the commit and the
    files the hooks changed. Raises git.GitError with the hooks' output otherwise."""
    repo_cfg = cfg.repos[slug]
    try:
        links = envs.link_envs(cfg, slug, worktree)
    except (envs.EnvError, OSError, subprocess.SubprocessError) as e:  # the hooks may still pass without them
        log.warning("Couldn't link dependencies for commit hooks: %s", e)
        links = []
    env = envs.process_env(repo_cfg)
    scope = paths if paths is not None else ["."]
    excluded = [f":(exclude){p}" for p in envs.provided_paths(repo_cfg)]

    def attempt() -> subprocess.CompletedProcess[str]:
        argv = ["git", "-C", str(worktree), "commit", "-m", message] + (["--", *paths] if paths is not None else [])
        try:
            return subprocess.run(argv, capture_output=True, text=True, errors="replace", env=env, timeout=HOOK_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired as e:
            raise git.GitError(f"The commit hooks didn't finish within {HOOK_TIMEOUT_S // 60} minutes; nothing was committed.") from e

    try:
        proc = attempt()
        fixed: list[str] = []
        if proc.returncode != 0:
            fixed = git._git(worktree, "diff", "--name-only", "--", *scope, *excluded).stdout.split()
            if fixed:
                git._git(worktree, "add", "--", *fixed)
                proc = attempt()
        if proc.returncode != 0:
            raise git.GitError(
                "Your repository's commit hooks failed, so nothing was committed. Fix what they report, then try again.\n\n"
                + (proc.stdout + proc.stderr).strip()[-4000:]
            )
        return git.head_sha(worktree), fixed
    finally:
        envs.unlink(links)


def apply_patch_and_commit(cfg: Config, pr: PullRequest, work: Path, patch: str, message: str) -> tuple[str, list[str]]:
    """Apply an agent's patch to your checkout and commit only its files, with your
    commit hooks. Refuses if those files have edits of yours. If the commit fails, the
    files are put back as they were, so nothing is left half-applied. A patch that a
    failed earlier attempt left staged is committed as it is."""
    paths = git.patch_paths(patch)
    already = git.staged_as(work, patch, paths)
    if not already:
        if dirty := git.dirty_paths(work, paths):
            raise git.GitError(
                "These files have uncommitted changes in your checkout, so the patch wasn't applied: "
                + ", ".join(dirty) + ". Commit or stash them, then try again."
            )
        git.apply_index(work, patch)
    try:
        return commit_with_hooks(cfg, pr.ref.slug, work, message, paths)
    except git.GitError:
        if not already:
            git.restore_paths(work, paths)
        raise


def is_created_by_reviewr(cfg: Config, pr: PullRequest, path: Path) -> bool:
    return path.resolve() == work_root(cfg, pr).resolve()


def work_status(cfg: Config, pr: PullRequest) -> dict:
    """What the Fix screen shows about the branch checkout."""
    path = existing_work_tree(cfg, pr)
    if path is None:
        return {"path": None, "branch": pr.head_ref, "created_by_reviewr": False}
    ahead, behind = git.ahead_behind(path, pr.head_oid)
    changed = git.patch_paths(git.working_diff(path, exclude=_excluded(cfg, pr)))
    return {
        "path": str(path),
        "branch": git.current_branch(path),
        "expected_branch": pr.head_ref,
        "created_by_reviewr": is_created_by_reviewr(cfg, pr, path),
        "ahead": ahead,
        "behind": behind,
        "uncommitted": changed,
    }


def _excluded(cfg: Config, pr: PullRequest) -> list[str]:
    """Env symlink paths, kept out of diffs and commits."""
    return envs.provided_paths(cfg.repos[pr.ref.slug])


# Agent drafts

def start_draft(store: Store, cfg: Config, pr: PullRequest, item_id: int) -> None:
    work = work_worktree(cfg, pr)

    def mark(s: ReviewState) -> None:
        item = s.item(item_id)
        item.status, item.error, item.patch, item.checks = "drafting", None, "", {}
        item.check_report, item.checks_running = None, False

    store.update_state(pr.ref, mark)
    _submit(pr, item_id, _draft_job, store, cfg, pr, item_id, work)


def _draft_job(store: Store, cfg: Config, pr: PullRequest, item_id: int, work: Path) -> None:
    repo_cfg = cfg.repos[pr.ref.slug]
    draft = _root(cfg, pr) / "drafts" / f"item-{item_id}"
    try:
        state = store.get_state(pr.ref)
        item = state.item(item_id)
        git.create_draft_worktree(repo_cfg.path, draft, git.head_sha(work))
        agent_log = run_agent(draft, prompt_for(pr, state, item), cfg.llm.model)
        patch = git.working_diff(draft, exclude=envs.provided_paths(repo_cfg))
        thread_checks = check_patch(pr, item.thread_ids, patch, cfg.llm)

        def drafted(s: ReviewState) -> None:
            it = s.item(item_id)
            it.patch, it.checks, it.agent_log, it.checks_running = patch, thread_checks, agent_log, True

        store.update_state(pr.ref, drafted)
        try:
            report = run_checks(cfg, pr.ref.slug, draft, patch) if repo_cfg.checks else None
        except Exception as e:  # e.g. installing the branch's dependencies failed
            log.exception("Checks for item %s failed to run", item_id)
            report = _failed_report(str(e))

        def done(s: ReviewState) -> None:
            it = s.item(item_id)
            it.status, it.check_report, it.checks_running = "ready", report, False

        store.update_state(pr.ref, done)
    except Exception as e:  # any failure is shown on the work item
        log.exception("Drafting item %s failed", item_id)
        message = str(e) or type(e).__name__

        def failed(s: ReviewState) -> None:
            it = s.item(item_id)
            it.status, it.error, it.checks_running = "failed", message, False

        store.update_state(pr.ref, failed)
    finally:
        git.remove_worktree(repo_cfg.path, draft)


def accept_patch(store: Store, cfg: Config, pr: PullRequest, item_id: int, message: str) -> ReviewState:
    work = work_worktree(cfg, pr)
    item = store.get_state(pr.ref).item(item_id)
    if item.status != "ready" or not item.patch:
        raise ValueError("This work item has no drafted patch to accept.")
    sha, _ = apply_patch_and_commit(cfg, pr, work, item.patch, message)

    def committed(s: ReviewState) -> None:
        it = s.item(item_id)
        it.status, it.commit_sha = "committed", sha

    return store.update_state(pr.ref, committed)


# Checks (lint and tests for the item's files)

def _failed_report(message: str) -> CheckReport:
    return CheckReport(
        ran_at=datetime.now(UTC), scope=[],
        runs=[CheckRun(name="Environment", kind="lint", status="error", reason=message)],
    )


def start_checks(store: Store, cfg: Config, pr: PullRequest, item_id: int) -> None:
    """Run checks in a throwaway worktree: on the agent's patch, or on a copy of your
    uncommitted changes (your own worktree and its .venv/node_modules are left alone)."""
    if not cfg.repos[pr.ref.slug].checks:
        raise ValueError(f'No checks are configured for {pr.ref.slug}. Add [[repos."{pr.ref.slug}".checks]] to reviewr.toml.')
    work = work_worktree(cfg, pr)
    store.update_state(pr.ref, lambda s: setattr(s.item(item_id), "checks_running", True))
    _submit(pr, item_id, _checks_job, store, cfg, pr, item_id, work)


def _checks_job(store: Store, cfg: Config, pr: PullRequest, item_id: int, work: Path) -> None:
    repo_cfg = cfg.repos[pr.ref.slug]
    item = store.get_state(pr.ref).item(item_id)
    scratch = _root(cfg, pr) / "drafts" / f"checks-{item_id}"
    report = None
    try:
        if item.mode == "agent" and item.patch:
            patch = item.patch
        else:
            patch = git.working_diff(work, exclude=envs.provided_paths(repo_cfg))
        git.create_draft_worktree(repo_cfg.path, scratch, git.head_sha(work))
        if patch.strip():
            git.apply_to_worktree(scratch, patch)
        report = run_checks(cfg, pr.ref.slug, scratch, patch)
    except Exception as e:
        log.exception("Checks for item %s failed to run", item_id)
        report = _failed_report(str(e))
    finally:
        git.remove_worktree(repo_cfg.path, scratch)

    def save(s: ReviewState) -> None:
        it = s.item(item_id)
        it.checks_running = False
        if report is not None:
            it.check_report = report

    store.update_state(pr.ref, save)


# Your own changes

def own_changes(cfg: Config, pr: PullRequest) -> str:
    return git.working_diff(work_worktree(cfg, pr), exclude=_excluded(cfg, pr))


def check_own_changes(store: Store, cfg: Config, pr: PullRequest, item_id: int) -> ReviewState:
    patch = own_changes(cfg, pr)
    item = store.get_state(pr.ref).item(item_id)
    checks = check_patch(pr, item.thread_ids, patch, cfg.llm)

    def save(s: ReviewState) -> None:
        s.item(item_id).checks = checks

    return store.update_state(pr.ref, save)


def commit_own_changes(store: Store, cfg: Config, pr: PullRequest, item_id: int, message: str, paths: list[str]) -> ReviewState:
    """Commit only the files you chose; your other uncommitted changes stay as they are."""
    excluded = set(_excluded(cfg, pr))
    sha = git.commit_paths(work_worktree(cfg, pr), message, [p for p in paths if p not in excluded])

    def committed(s: ReviewState) -> None:
        it = s.item(item_id)
        it.status, it.commit_sha = "committed", sha

    return store.update_state(pr.ref, committed)
