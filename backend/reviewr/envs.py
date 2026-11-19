"""Dependency environments built from the branch under review.

Each configured environment (e.g. the backend virtualenv, the frontend node_modules)
is installed from the lockfiles *at the branch being reviewed*, never from your clone:

  <data_dir>/envs/<owner>__<repo>/<name>-<hash>/

where <hash> covers the lockfiles' contents, so branches with the same lockfiles share
one install and a branch that changes them gets a new one. The lockfiles are copied
into that folder at their repo paths and the install commands run there. Worktrees
then get symlinks at the paths the repo expects (`provides`), and those symlinks are
kept out of patches and commits.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from reviewr.config import Config, EnvConfig, RepoConfig

Reader = Callable[[str], bytes | None]  # repo-relative path -> file contents, or None


class EnvError(RuntimeError):
    pass


class EnvStatus(BaseModel):
    name: str
    key: str
    status: Literal["ready", "missing", "installing", "failed"]
    path: str
    log_tail: str = ""


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(path: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(str(path), threading.Lock())


def worktree_reader(worktree: Path) -> Reader:
    def read(rel: str) -> bytes | None:
        p = worktree / rel
        return p.read_bytes() if p.is_file() else None
    return read


def commit_reader(repo: Path, oid: str) -> Reader:
    def read(rel: str) -> bytes | None:
        proc = subprocess.run(["git", "-C", str(repo), "show", f"{oid}:{rel}"], capture_output=True, check=False)
        return proc.stdout if proc.returncode == 0 else None
    return read


def env_key(env: EnvConfig, read: Reader) -> str:
    h = hashlib.sha256()
    for rel in sorted(env.files):
        h.update(rel.encode() + b"\0" + (read(rel) or b"<missing>") + b"\0")
    for argv in env.install:
        h.update(" ".join(argv).encode() + b"\0")
    return h.hexdigest()[:12]


def env_dir(cfg: Config, slug: str, env: EnvConfig, key: str) -> Path:
    return cfg.data_dir / "envs" / slug.replace("/", "__") / f"{env.name}-{key}"


def process_env(repo: RepoConfig) -> dict[str, str]:
    """Environment for installs and checks: `path_prepend` goes before PATH (e.g. the
    Node version the repo's .nvmrc asks for)."""
    env = dict(os.environ)
    extra = [str(Path(p).expanduser()) for p in repo.path_prepend]
    if extra:
        env["PATH"] = os.pathsep.join([*extra, env.get("PATH", "")])
    return env


def status(cfg: Config, slug: str, read: Reader) -> list[EnvStatus]:
    repo = cfg.repos[slug]
    out = []
    for env in repo.envs:
        key = env_key(env, read)
        d = env_dir(cfg, slug, env, key)
        if (d / ".reviewr-ready").exists():
            st = "ready"
        elif _lock(d).locked():
            st = "installing"
        elif (d / ".reviewr-failed").exists():
            st = "failed"
        else:
            st = "missing"
        log = (d / ".reviewr-install.log")
        tail = log.read_text(errors="replace")[-3000:] if log.exists() and st != "ready" else ""
        out.append(EnvStatus(name=env.name, key=key, status=st, path=str(d), log_tail=tail))
    return out


def _touch_used(d: Path) -> None:
    (d / ".reviewr-last-used").touch()


def _latest_ready(cfg: Config, slug: str, env: EnvConfig, exclude: Path) -> Path | None:
    """The most recently used finished install of this env, to seed a new one from."""
    root = cfg.data_dir / "envs" / slug.replace("/", "__")
    candidates = [
        p for p in root.glob(f"{env.name}-*")
        if p != exclude and (p / ".reviewr-ready").exists()
    ]
    return max(candidates, key=lambda p: _last_used(p), default=None)


def _last_used(d: Path) -> float:
    marker = d / ".reviewr-last-used"
    return marker.stat().st_mtime if marker.exists() else d.stat().st_mtime


def _clone_tree(src: Path, dst: Path) -> None:
    """Copy-on-write copy where the filesystem supports it (APFS clonefile, btrfs/XFS
    reflinks), so a seeded install shares disk blocks with the one it came from."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        argv = ["cp", "-c", "-R", str(src), str(dst)]
    else:
        argv = ["cp", "-a", "--reflink=auto", str(src), str(dst)]
    if subprocess.run(argv, capture_output=True, check=False).returncode != 0:
        shutil.copytree(src, dst, symlinks=True)


def ensure(cfg: Config, slug: str, env: EnvConfig, read: Reader) -> Path:
    """Install `env` for these lockfiles if it isn't already. Blocks while installing.

    When the env has `seed` paths and an earlier install exists, the new one starts as a
    copy-on-write clone of it and runs `install_seeded`, which only applies the
    differences, instead of installing everything from scratch.
    """
    repo = cfg.repos[slug]
    key = env_key(env, read)
    d = env_dir(cfg, slug, env, key)
    with _lock(d):
        if (d / ".reviewr-ready").exists():
            _touch_used(d)
            return d
        if d.exists():
            shutil.rmtree(d)  # a half-finished or failed install
        d.mkdir(parents=True)
        for rel in env.files:
            data = read(rel)
            if data is not None:
                (d / rel).parent.mkdir(parents=True, exist_ok=True)
                (d / rel).write_bytes(data)
        log_path = d / ".reviewr-install.log"
        with log_path.open("w") as log:
            commands = env.install
            seed_from = _latest_ready(cfg, slug, env, d) if env.seed and env.install_seeded else None
            if seed_from:
                for rel in env.seed:
                    if (seed_from / rel).exists():
                        _clone_tree(seed_from / rel, d / rel)
                log.write(f"Seeded from {seed_from.name}; applying only the differences.\n")
                commands = env.install_seeded or env.install
            for argv in commands:
                log.write(f"$ {' '.join(argv)}\n")
                log.flush()
                try:
                    proc = subprocess.run(
                        [a.replace("{env}", str(d)) for a in argv], cwd=d, stdout=log, stderr=subprocess.STDOUT,
                        env=process_env(repo), timeout=env.timeout, check=False,
                    )
                except (FileNotFoundError, subprocess.TimeoutExpired) as e:
                    log.write(f"\n{e}\n")
                    (d / ".reviewr-failed").touch()
                    raise EnvError(f"Installing {env.name} failed: {e}") from e
                if proc.returncode != 0:
                    (d / ".reviewr-failed").touch()
                    raise EnvError(f"Installing {env.name} failed (exit {proc.returncode}). See {log_path}")
        (d / ".reviewr-ready").touch()
        _touch_used(d)
        return d


def link_envs(cfg: Config, slug: str, worktree: Path) -> list[Path]:
    """Install (if needed) every env from the worktree's own lockfiles and symlink
    them in. Returns the links made, for `unlink`."""
    made: list[Path] = []
    read = worktree_reader(worktree)
    for env in cfg.repos[slug].envs:
        d = ensure(cfg, slug, env, read)
        for in_worktree, in_env in env.provides.items():
            src, dst = d / in_env, worktree / in_worktree
            if not src.exists():
                raise EnvError(f"{env.name}: install didn't create {in_env}")
            if dst.is_symlink():
                dst.unlink()
            if dst.exists():
                continue  # a real folder there (e.g. you installed into reviewr's worktree yourself)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(src, target_is_directory=True)
            made.append(dst)
    return made


def unlink(links: list[Path]) -> None:
    for link in links:
        if link.is_symlink():
            link.unlink()


def provided_paths(repo: RepoConfig) -> list[str]:
    """Paths env symlinks occupy in worktrees; kept out of diffs and commits."""
    return [p for env in repo.envs for p in env.provides]


# Inventory and cleanup

class InstalledEnv(BaseModel):
    slug: str
    name: str
    key: str
    path: str
    size_mb: int
    last_used: float
    status: Literal["ready", "failed", "partial"]


def _size_mb(d: Path) -> int:
    """Disk usage as `du` reports it. Copy-on-write clones count in full here even
    though they share blocks, so this overstates the real total."""
    proc = subprocess.run(["/usr/bin/du", "-sk", str(d)], capture_output=True, text=True, check=False)
    try:
        return int(proc.stdout.split()[0]) // 1024
    except (IndexError, ValueError):
        return 0


def inventory(cfg: Config) -> list[InstalledEnv]:
    out = []
    root = cfg.data_dir / "envs"
    for repo_dir in sorted(root.glob("*")) if root.exists() else []:
        slug = repo_dir.name.replace("__", "/", 1)
        for d in sorted(repo_dir.iterdir()):
            if not d.is_dir():
                continue
            name, _, key = d.name.rpartition("-")
            st = "ready" if (d / ".reviewr-ready").exists() else "failed" if (d / ".reviewr-failed").exists() else "partial"
            out.append(InstalledEnv(slug=slug, name=name, key=key, path=str(d), size_mb=_size_mb(d), last_used=_last_used(d), status=st))
    return out


def remove(paths: list[str], cfg: Config) -> list[str]:
    """Delete installs (only ones under reviewr's env folder, and not mid-install)."""
    root = (cfg.data_dir / "envs").resolve()
    removed = []
    for p in paths:
        d = Path(p).resolve()
        if not d.is_relative_to(root) or d == root:
            continue
        lock = _lock(d)
        if not lock.acquire(blocking=False):
            continue
        try:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(str(d))
        finally:
            lock.release()
    return removed


def age_days(env: InstalledEnv) -> float:
    return (time.time() - env.last_used) / 86400
