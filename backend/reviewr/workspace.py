"""Workspace setup: get a PR's repository ready to work in.

When you open a PR whose repo reviewr has no clone of, the app starts here. Setup runs as
a list of steps you can watch; steps that don't depend on each other run at the same time:

  1. clone     find a clone of the repo under the workspace folder, or clone it to
               <workspace>/<owner>/<repo>; record it in the settings
  2. fetch     fetch the PR's base branch and head commit
  3. branch    a checkout of the PR branch (yours, or a worktree reviewr creates)
  4. node, python    the versions the repo asks for, found or installed   } in parallel
  5. detect    dependency installs and checks, from the files on the PR branch
               (only for what the repo doesn't have configured yet)
  6. install:<name>  each dependency install, from the branch's lockfiles  } in parallel

The session is kept in <data_dir>/setup/<owner>__<repo>.json so the page survives a
reload; one left "running" by a restart is marked interrupted.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from reviewr import detect, envs, fixwork, git, toolchains
from reviewr.config import Config, RepoConfig, load_config, update_config
from reviewr.launch import github_slug
from reviewr.models import PRRef, PullRequest

log = logging.getLogger(__name__)

CLONE_TIMEOUT_S = 3600
FETCH_TIMEOUT_S = 600

StepStatus = Literal["pending", "running", "done", "skipped", "warning", "failed"]


class Step(BaseModel):
    id: str
    title: str
    status: StepStatus = "pending"
    detail: str = ""
    log: str = ""


class SetupSession(BaseModel):
    slug: str
    number: int
    status: Literal["running", "done", "failed"] = "running"
    started_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    steps: list[Step] = Field(default_factory=list)


_lock = threading.Lock()
_running: set[str] = set()


def _path(cfg: Config, slug: str) -> Path:
    return cfg.data_dir / "setup" / f"{slug.replace('/', '__')}.json"


def load(cfg: Config, slug: str) -> SetupSession | None:
    p = _path(cfg, slug)
    if not p.is_file():
        return None
    try:
        s = SetupSession.model_validate_json(p.read_text())
    except ValueError:
        return None
    if s.status == "running" and slug not in _running:
        s.status, s.error = "failed", "Interrupted because reviewr restarted. Run setup again."
        for step in s.steps:
            if step.status in ("running", "pending"):
                step.status = "failed" if step.status == "running" else "pending"
    return s


def _save(cfg: Config, s: SetupSession) -> None:
    p = _path(cfg, s.slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s.model_dump_json(indent=2))


def needed(cfg: Config, slug: str) -> str | None:
    """Why the repo needs setting up, or None if it has a clone."""
    repo = cfg.repos.get(slug)
    if repo is None:
        return f"reviewr doesn't have a clone of {slug} yet."
    if not (repo.path / ".git").exists():
        return f"The clone of {slug} at {repo.path} isn't there anymore."
    return None


# Finding or making the clone

def find_clone(workspace: Path, slug: str, max_depth: int = 3) -> Path | None:
    """A clone of `slug` under the workspace folder (any origin URL form), if there is one."""
    if not workspace.is_dir():
        return None
    preferred = workspace / slug
    candidates = [preferred] if (preferred / ".git").exists() else []
    level = [workspace]
    for _ in range(max_depth):
        nxt = []
        for d in level:
            try:
                children = [c for c in d.iterdir() if c.is_dir() and not c.name.startswith(".") and c.name != "node_modules"]
            except OSError:
                continue
            for c in children:
                if (c / ".git").exists():
                    candidates.append(c)
                else:
                    nxt.append(c)
        level = nxt
    for c in candidates:
        out = subprocess.run(["git", "-C", str(c), "remote", "-v"], capture_output=True, text=True, check=False).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and (s := github_slug(parts[1])) and s.lower() == slug.lower():
                return c
    return None


class Runner:
    """Runs one setup session, saving it as each step changes."""

    def __init__(self, cfg: Config, pr: PullRequest):
        self.cfg, self.pr = cfg, pr
        self.slug = pr.ref.slug
        self.s = SetupSession(slug=self.slug, number=pr.ref.number, started_at=datetime.now(UTC), steps=[
            Step(id="clone", title="Find or clone the repository"),
            Step(id="fetch", title="Fetch the pull request"),
            Step(id="branch", title=f"Check out {pr.head_ref}"),
            Step(id="node", title="Node.js version"),
            Step(id="python", title="Python version"),
            Step(id="detect", title="Find dependencies and checks"),
        ])
        self._m = threading.Lock()

    def step(self, sid: str) -> Step:
        return next(s for s in self.s.steps if s.id == sid)

    def set(self, sid: str, status: StepStatus | None = None, detail: str | None = None, log_text: str | None = None) -> None:
        with self._m:
            st = self.step(sid)
            if status is not None:
                st.status = status
            if detail is not None:
                st.detail = detail
            if log_text is not None:
                st.log = log_text[-4000:]
            _save(self.cfg, self.s)

    def run(self) -> None:
        try:
            self._run()
            self.s.status = "failed" if any(st.status == "failed" for st in self.s.steps) else "done"
            if self.s.status == "failed":
                self.s.error = "Some steps failed. Fix what they report, then run setup again."
        except Exception as e:  # shown on the setup page
            log.exception("Workspace setup for %s failed", self.slug)
            self.s.status, self.s.error = "failed", str(e) or type(e).__name__
            for st in self.s.steps:
                if st.status == "running":
                    st.status = "failed"
        finally:
            self.s.finished_at = datetime.now(UTC)
            _save(self.cfg, self.s)
            _running.discard(self.slug)

    def _run(self) -> None:
        clone = self._clone()
        self._fetch(clone)
        cfg = load_config()  # now has the repo
        self.set("branch", "running")
        work = fixwork.work_worktree(cfg, self.pr)
        self.set("branch", "done", str(work))
        read = envs.commit_reader(clone, self.pr.head_oid)

        with ThreadPoolExecutor(max_workers=2) as pool:
            node_f = pool.submit(self._toolchain, "node", read)
            py_f = pool.submit(self._toolchain, "python", read)
            node, python = node_f.result(), py_f.result()

        self.set("detect", "running")
        repo = load_config().repos[self.slug]
        files = subprocess.run(["git", "-C", str(clone), "ls-tree", "-r", "--name-only", self.pr.head_oid],
                               capture_output=True, text=True, check=True).stdout.splitlines()
        found_envs, notes = detect.detect_envs(files, read, python.version if python.ok else None)
        new_envs = repo.envs or found_envs
        new_checks = repo.checks or detect.detect_checks(new_envs, files, read)

        def record(c: Config) -> None:
            r = c.repos[self.slug]
            if not r.envs:
                r.envs = found_envs
            if not r.checks:
                r.checks = new_checks
            r.toolchain = {t.name: t.version for t in (node, python) if t.version}
            if node.ok and node.bin_dir:
                r.path_prepend = [node.bin_dir, *[p for p in r.path_prepend if p != node.bin_dir and "/node" not in p]]

        update_config(record)
        parts = []
        parts.append(("Using your configured installs: " if repo.envs else "Installs: ") + (", ".join(e.name for e in new_envs) or "none found"))
        parts.append(("Using your configured checks: " if repo.checks else "Checks: ") + (", ".join(c.name for c in new_checks) or "none found"))
        self.set("detect", "warning" if notes else "done", ". ".join(parts) + ("\n" + "\n".join(notes) if notes else ""))

        cfg = load_config()
        with self._m:
            self.s.steps += [Step(id=f"install:{e.name}", title=f"Install {e.name} dependencies") for e in cfg.repos[self.slug].envs]
            _save(self.cfg, self.s)
        with ThreadPoolExecutor(max_workers=4) as pool:
            for f in [pool.submit(self._install, cfg, e, read) for e in cfg.repos[self.slug].envs]:
                f.result()

    def _clone(self) -> Path:
        self.set("clone", "running")
        cfg = load_config()
        repo = cfg.repos.get(self.slug)
        if repo and (repo.path / ".git").exists():
            self.set("clone", "done", f"Using your clone at {repo.path}")
            return repo.path
        found = find_clone(cfg.workspace_dir, self.slug)
        if found is not None:
            detail = f"Found a clone at {found}"
        else:
            found = cfg.workspace_dir / self.pr.ref.owner / self.pr.ref.repo
            if found.exists() and any(found.iterdir()):
                raise RuntimeError(f"{found} already exists and isn't a clone of {self.slug}. Move it, or change the workspace folder in Settings.")
            found.parent.mkdir(parents=True, exist_ok=True)
            self.set("clone", detail=f"Cloning into {found}…")
            proc = subprocess.run(
                ["gh", "repo", "clone", self.slug, str(found), "--", "--filter=blob:none"],
                capture_output=True, text=True, timeout=CLONE_TIMEOUT_S, check=False,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
            if proc.returncode != 0:
                self.set("clone", "failed", "Cloning failed.", proc.stderr)
                raise RuntimeError(f"Couldn't clone {self.slug}: {proc.stderr.strip()[-500:]}")
            detail = f"Cloned into {found}"

        def add(c: Config) -> None:
            existing = c.repos.get(self.slug)
            c.repos[self.slug] = existing.model_copy(update={"path": found}) if existing else RepoConfig(path=found)

        update_config(add)
        self.set("clone", "done", detail)
        return found

    def _fetch(self, clone: Path) -> None:
        self.set("fetch", "running")
        refs = [self.pr.base_ref, f"+refs/pull/{self.pr.ref.number}/head:refs/remotes/origin/pr/{self.pr.ref.number}"]
        if not git.has_commit(clone, self.pr.head_oid) or not git.has_commit(clone, self.pr.base_oid):
            proc = subprocess.run(["git", "-C", str(clone), "fetch", "--quiet", "origin", *refs],
                                  capture_output=True, text=True, timeout=FETCH_TIMEOUT_S, check=False,
                                  env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
            if proc.returncode != 0:
                self.set("fetch", "failed", "Fetching failed.", proc.stderr)
                raise RuntimeError(f"Couldn't fetch the PR: {proc.stderr.strip()[-500:]}")
        git.ensure_commits(clone, self.pr.base_oid, self.pr.head_oid)
        self.set("fetch", "done", f"{self.pr.base_ref} and {self.pr.head_oid[:10]}")

    def _toolchain(self, name: str, read) -> toolchains.Toolchain:
        self.set(name, "running")
        t = toolchains.resolve_node(read) if name == "node" else toolchains.resolve_python(read)
        if t.requested is None and t.version is None and t.ok is False and name == "node":
            # A repo with no JavaScript doesn't need Node.
            self.set(name, "skipped", t.note)
            return t
        asked = f"{t.requested} (from {t.source})" if t.requested else "not pinned"
        if t.ok:
            detail = f"Asked for: {asked}. Using {t.version}" + (f" at {t.bin_dir}" if t.bin_dir else "") + "."
            self.set(name, "warning" if t.note else "done", detail + (f" {t.note}" if t.note else ""))
        else:
            self.set(name, "warning", f"Asked for: {asked}. {t.note}")
        return t

    def _install(self, cfg: Config, env, read) -> None:
        sid = f"install:{env.name}"
        self.set(sid, "running")
        try:
            d = envs.ensure(cfg, self.slug, env, read)
        except envs.EnvError as e:
            st = next((x for x in envs.status(cfg, self.slug, read) if x.name == env.name), None)
            self.set(sid, "failed", str(e), st.log_tail if st else "")
            return
        self.set(sid, "done", f"Installed in {d}")


def start(cfg: Config, pr: PullRequest) -> SetupSession:
    """Start setup in the background (or return the one already running)."""
    with _lock:
        if pr.ref.slug in _running:
            return load(cfg, pr.ref.slug)  # type: ignore[return-value]
        _running.add(pr.ref.slug)
    runner = Runner(cfg, pr)
    _save(cfg, runner.s)
    threading.Thread(target=runner.run, name=f"setup-{pr.ref.slug}", daemon=True).start()
    return runner.s


def view(cfg: Config, ref: PRRef) -> dict:
    s = load(cfg, ref.slug)
    return {
        "needed": needed(cfg, ref.slug),
        "running": ref.slug in _running,
        "workspace_dir": str(cfg.workspace_dir),
        "session": s.model_dump(mode="json") if s else None,
    }
