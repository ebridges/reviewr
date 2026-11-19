"""Free disk space: dependency installs and worktrees that no open PR needs.

`plan` is a dry run and lists everything it would remove and why; `run` removes exactly
what the plan lists. Removed:
- installs (envs.py) that no open PR's lockfiles map to, and failed or half-finished ones;
- worktrees reviewr created for PRs that are merged or closed, but only when the branch
  has no commits beyond the PR head and no uncommitted changes, so no work is lost
  (your own worktrees and local branches are never removed);
- leftover draft worktrees older than a day (normally removed when a draft finishes).
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from pydantic import BaseModel

from reviewr import envs, fixwork, git
from reviewr.config import Config
from reviewr.github.client import GitHubClient, GitHubError
from reviewr.models import PullRequest
from reviewr.store import Store

DRAFT_MAX_AGE_S = 86400


class EnvItem(BaseModel):
    path: str
    label: str
    size_mb: int
    reason: str


class WorktreeItem(BaseModel):
    path: str
    repo: str
    branch: str | None
    pr: str
    reason: str


class CleanupPlan(BaseModel):
    envs_remove: list[EnvItem]
    envs_keep: list[EnvItem]
    worktrees_remove: list[WorktreeItem]
    worktrees_keep: list[WorktreeItem]
    open_prs: list[str]
    unknown_prs: list[str]  # couldn't get state from GitHub; treated as open (kept)

    @property
    def size_mb(self) -> int:
        return sum(e.size_mb for e in self.envs_remove)


def _pr_state(gh: GitHubClient, pr: PullRequest) -> str | None:
    try:
        data = gh.query("pr_state", owner=pr.ref.owner, repo=pr.ref.repo, number=pr.ref.number)
    except GitHubError:
        return None
    node = (data.get("repository") or {}).get("pullRequest")
    return node["state"] if node else None


def _reader_for(cfg: Config, pr: PullRequest) -> envs.Reader | None:
    work = fixwork.existing_work_tree(cfg, pr)
    if work:
        return envs.worktree_reader(work)
    repo = cfg.repos[pr.ref.slug].path
    return envs.commit_reader(repo, pr.head_oid) if git.has_commit(repo, pr.head_oid) else None


def _clean_worktree(path: Path) -> bool:
    proc = subprocess.run(["git", "-C", str(path), "status", "--porcelain"], capture_output=True, text=True, check=False)
    return proc.returncode == 0 and not proc.stdout.strip()


def plan(cfg: Config, store: Store, gh: GitHubClient) -> CleanupPlan:
    keep_dirs: dict[str, str] = {}  # env dir -> PR that needs it
    open_prs, unknown = [], []
    wt_remove, wt_keep = [], []

    for pr in store.list_prs():
        if pr.ref.slug not in cfg.repos:
            continue
        state = _pr_state(gh, pr)
        is_open = state in (None, "OPEN")
        (unknown if state is None else open_prs if is_open else []).append(pr.ref.key)
        work = fixwork.work_root(cfg, pr)
        if is_open:
            read = _reader_for(cfg, pr)
            if read:
                for env in cfg.repos[pr.ref.slug].envs:
                    keep_dirs[str(envs.env_dir(cfg, pr.ref.slug, env, envs.env_key(env, read)))] = pr.ref.key
            if work.exists():
                wt_keep.append(WorktreeItem(path=str(work), repo=pr.ref.slug, branch=None, pr=pr.ref.key, reason="PR is open"))
            continue
        if not work.exists():
            continue
        ahead = git.commits_ahead(work, pr.head_oid)
        item = WorktreeItem(path=str(work), repo=pr.ref.slug, branch=None, pr=pr.ref.key, reason="")
        if ahead:
            wt_keep.append(item.model_copy(update={"reason": f"PR is {state.lower()}, but the branch has {ahead} commit(s) that were never pushed"}))
        elif not _clean_worktree(work):
            wt_keep.append(item.model_copy(update={"reason": f"PR is {state.lower()}, but the worktree has uncommitted changes"}))
        else:
            wt_remove.append(item.model_copy(update={"reason": f"PR is {state.lower()} and nothing is unpushed"}))

    # Leftover draft worktrees (a crashed draft job).
    now = time.time()
    for drafts in (cfg.data_dir / "worktrees").glob("*/drafts/*"):
        if drafts.is_dir() and now - drafts.stat().st_mtime > DRAFT_MAX_AGE_S:
            wt_remove.append(WorktreeItem(path=str(drafts), repo="", branch=None, pr=drafts.parent.parent.name, reason="leftover draft worktree"))

    env_remove, env_keep = [], []
    for inst in envs.inventory(cfg):
        label = f"{inst.slug} · {inst.name} · {inst.key}"
        days = envs.age_days(inst)
        used = f"last used {days:.0f} day{'s' if round(days) != 1 else ''} ago"
        if inst.status != "ready":
            env_remove.append(EnvItem(path=inst.path, label=label, size_mb=inst.size_mb, reason=f"{inst.status} install"))
        elif inst.path in keep_dirs:
            env_keep.append(EnvItem(path=inst.path, label=label, size_mb=inst.size_mb, reason=f"needed by {keep_dirs[inst.path]}"))
        else:
            env_remove.append(EnvItem(path=inst.path, label=label, size_mb=inst.size_mb, reason=f"no open PR needs it; {used}"))

    return CleanupPlan(
        envs_remove=env_remove, envs_keep=env_keep, worktrees_remove=wt_remove, worktrees_keep=wt_keep,
        open_prs=open_prs, unknown_prs=unknown,
    )


def run(cfg: Config, p: CleanupPlan) -> list[str]:
    done = [f"Removed install {Path(x).name}" for x in envs.remove([e.path for e in p.envs_remove], cfg)]
    for w in p.worktrees_remove:
        path = Path(w.path)
        repo = cfg.repos[w.repo].path if w.repo in cfg.repos else None
        if repo:
            git.remove_worktree(repo, path)
            if w.branch:
                subprocess.run(["git", "-C", str(repo), "branch", "-D", w.branch], capture_output=True, check=False)
        if path.exists() and path.is_relative_to(cfg.data_dir):
            shutil.rmtree(path, ignore_errors=True)
        done.append(f"Removed worktree {path.name} ({w.pr})")
    for repo_cfg in cfg.repos.values():
        if repo_cfg.path.exists():
            subprocess.run(["git", "-C", str(repo_cfg.path), "worktree", "prune"], capture_output=True, check=False)
    return done
