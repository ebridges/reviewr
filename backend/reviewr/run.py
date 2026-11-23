"""Run the PR's app: its services at the PR branch, started, watched and stopped by reviewr.

You choose what runs each time:
  github    the PR head as it is on GitHub, in a detached worktree reviewr keeps at
            <data_dir>/worktrees/<owner>__<repo>__<n>/run, so your edits aren't touched
  checkout  your checkout of the branch, with unpushed commits and uncommitted edits

How to run it comes from the repo's saved RunConfig (config.py; first proposed by
detect.detect_run). A start goes through these steps, which the Run page polls:

  1. worktree   the code to run
  2. deps       dependency installs from that worktree's lockfiles, linked in (envs.py)
  3. env files  files like .env copied from your clone when missing, only if git ignores them
  4. compose    docker compose up -d, under a project name of its own
  5. setup:*    one-off commands (migrations, seed), in order
  6. services   each started in its own process group, logging to a file; ready when
                its port accepts connections

Database guard: setup commands usually change a database. Before they run, the
database hosts in the run's environment (RunConfig.env over the env files over
reviewr's own environment) are checked. Unless every one is local, start() raises
SetupNeedsConfirm and nothing has started; you then run them anyway, or skip them.

Sessions are kept in <data_dir>/runs/<owner>__<repo>__<n>.json, logs next to them. After
a restart of reviewr, services whose process is still alive show as running; Stop
still works, by process group.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from reviewr import envs, fixwork, git
from reviewr.config import Config, RunConfig, ServiceConfig
from reviewr.models import PullRequest
from reviewr.workspace import Step, StepStatus

log = logging.getLogger(__name__)

Target = Literal["github", "checkout"]
STOP_GRACE_S = 10
LOG_TAIL = 4000


class RunError(RuntimeError):
    pass


class DatabaseTarget(BaseModel):
    var: str
    host: str
    local: bool


class SetupNeedsConfirm(RunError):
    """Setup commands would run against a database that isn't (or might not be) local."""

    def __init__(self, commands: list[str], databases: list[DatabaseTarget]):
        self.commands, self.databases = commands, databases
        if databases:
            where = ", ".join(f"{d.var} → {d.host}" for d in databases if not d.local)
            why = f"they would use a database that isn't local ({where})"
        else:
            why = "reviewr couldn't tell which database they use"
        super().__init__(
            f"The setup commands ({', '.join(commands)}) usually change a database, and {why}. "
            "Run them anyway, skip them this time, or set RunConfig env overrides in Settings to point the app at its own database."
        )


class ServiceState(BaseModel):
    name: str
    status: Literal["starting", "running", "exited", "stopped"] = "starting"
    pid: int | None = None
    port: int | None = None
    url: str | None = None
    open: bool = False
    log_path: str
    exit_code: int | None = None
    note: str | None = None  # e.g. not answering on its port yet


class RunSession(BaseModel):
    slug: str
    number: int
    target: Target
    worktree: str
    head_sha: str
    started_at: datetime
    status: Literal["starting", "running", "stopping", "stopped", "failed"] = "starting"
    error: str | None = None
    steps: list[Step] = Field(default_factory=list)
    services: list[ServiceState] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)  # env symlinks to undo on stop
    compose_project: str | None = None
    compose_file: str | None = None
    setup_skipped: bool = False


_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()
# Processes started by this reviewr: (session key, service name or "setup:<name>") -> process.
# Registered under the session's lock, so a stop holding it sees every one.
_procs: dict[tuple[str, str], subprocess.Popen] = {}
_jobs: set[str] = set()  # sessions with a start job running in this process
_stop_requested: set[str] = set()  # sessions being stopped; their start job saves nothing more
_started: dict[Path, Config] = {}  # data_dir -> config, for each place this process started a run


def _key(pr: PullRequest) -> str:
    return f"{pr.ref.owner}__{pr.ref.repo}__{pr.ref.number}"


def _lock(key: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


def runs_dir(cfg: Config) -> Path:
    return cfg.data_dir / "runs"


def _path(cfg: Config, key: str) -> Path:
    return runs_dir(cfg) / f"{key}.json"


def _save(cfg: Config, key: str, s: RunSession) -> None:
    p = _path(cfg, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(s.model_dump_json(indent=2))
    tmp.replace(p)


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A child of this process that exited is a zombie until collected, and still answers
    # kill(pid, 0). Collect it: through its Popen if there is one, else with waitpid.
    for proc in list(_procs.values()):
        if proc.pid == pid:
            return proc.poll() is None
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:  # not our child (e.g. started before reviewr restarted)
        return True


def load(cfg: Config, pr: PullRequest) -> RunSession | None:
    """The PR's run, with services whose process is gone marked exited or stopped."""
    p = _path(cfg, _key(pr))
    if not p.exists():
        return None
    try:
        s = RunSession.model_validate_json(p.read_text())
    except ValueError:
        return None
    key = _key(pr)
    if key in _jobs or key in _stop_requested:
        return s  # the job (or a stop) in this process is keeping it up to date
    changed = False
    for svc in s.services:
        if svc.pid and svc.status in ("starting", "running") and not _alive(svc.pid):
            proc = _procs.get((key, svc.name))
            svc.status, svc.exit_code = "exited", proc.returncode if proc else None
            changed = True
    live = any(svc.pid and svc.status in ("starting", "running") for svc in s.services)
    if s.status in ("starting", "stopping"):  # no job here: cut off by a restart of reviewr
        s.status = "running" if live else "failed"
        s.error = s.error or "Interrupted because reviewr restarted. Stop it and start again if anything's missing."
        for st in s.steps:
            if st.status == "running":
                st.status = "failed"
        changed = True
    elif s.status == "running" and not live:
        s.status, s.error = "stopped", s.error or "Every service has exited; see their logs."
        changed = True
    if changed:
        _save(cfg, key, s)
    return s


# Worktree and environment

def run_root(cfg: Config, pr: PullRequest) -> Path:
    """Where the `github` target's worktree lives."""
    return fixwork._root(cfg, pr) / "run"


def _github_worktree(cfg: Config, pr: PullRequest) -> Path:
    repo = cfg.repo_path(pr.ref.slug)
    git.ensure_commits(repo, pr.head_oid)
    path = run_root(cfg, pr)
    if (path / ".git").exists():
        # Keep untracked files (copied env files, build caches); tracked files follow the PR head.
        proc = git._git(path, "checkout", "--detach", "--force", pr.head_oid, check=False)
        if proc.returncode == 0:
            return path
    return git.create_draft_worktree(repo, path, pr.head_oid)


def parse_env_file(text: str) -> dict[str, str]:
    """KEY=value lines of a .env file (export prefix, quotes and comments handled)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        k, sep, v = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k.strip()):
            continue
        v = v.strip()
        if v[:1] in ("'", '"') and (end := v.find(v[0], 1)) > 0:
            v = v[1:end]  # quoted; anything after the closing quote is a comment
        else:
            v = re.sub(r"\s+#.*$", "", v)
        out[k.strip()] = v
    return out


def copy_env_files(cfg: Config, slug: str, worktree: Path, files: list[str]) -> list[str]:
    """Copy env files from your clone into the worktree when they're missing there.
    Never overwrites, and only copies files git ignores in the worktree, so a secret
    can't end up in a commit. Returns notes on what was done."""
    clone = cfg.repo_path(slug)
    notes = []
    for rel in files:
        dst, src = worktree / rel, clone / rel
        if dst.exists():
            notes.append(f"{rel}: already there")
        elif not src.is_file():
            notes.append(f"{rel}: not in your clone ({clone}), so not copied")
        elif git._git(worktree, "check-ignore", "-q", "--", rel, check=False).returncode != 0:
            notes.append(f"{rel}: git doesn't ignore it here, so it wasn't copied (it could end up in a commit)")
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            notes.append(f"{rel}: copied from your clone")
    return notes


def file_env(worktree: Path, files: list[str]) -> dict[str, str]:
    """Variables the app would read from its env files."""
    out: dict[str, str] = {}
    for rel in files:
        p = worktree / rel
        if p.is_file():
            out.update(parse_env_file(p.read_text(errors="replace")))
    return out


def process_env(cfg: Config, slug: str, run: RunConfig, extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for setup commands, compose and services: reviewr's (with the
    repo's toolchain first on PATH), then RunConfig.env, then the service's own.
    UV_NO_SYNC keeps `uv run` from changing the shared, linked-in virtualenv."""
    env = envs.process_env(cfg.repos[slug])
    env["UV_NO_SYNC"] = "1"
    env.update(run.env)
    env.update(extra or {})
    return env


# The database guard

_DB_SCHEMES = ("postgres", "postgresql", "mysql", "mariadb", "mongodb", "mongodb+srv", "redis", "rediss", "mssql",
               "sqlserver", "cockroachdb", "oracle")
_LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}
_HOST_VAR = re.compile(r"(?:^|_)(?:DB|DATABASE|POSTGRES|PG|MYSQL|MARIADB|MONGO|MONGODB|REDIS)\w*HOST$|^PGHOST$")


def database_targets(env: dict[str, str], compose_services: set[str] = frozenset()) -> list[DatabaseTarget]:
    """The database hosts an environment points at: *_URL/_URI/_DSN values with a
    database scheme (and anything named *DATABASE_URL*), and *DB*_HOST-style variables."""
    out = []
    for var, value in sorted(env.items()):
        host = None
        if re.search(r"(_URL|_URI|_DSN)$", var) or "DATABASE_URL" in var:
            u = urlparse(value)
            scheme = u.scheme.split("+")[0].lower()
            if scheme == "sqlite":
                out.append(DatabaseTarget(var=var, host="sqlite file", local=True))
                continue
            if scheme in _DB_SCHEMES or (scheme and "DATABASE_URL" in var):
                host = u.hostname or ""
        elif _HOST_VAR.search(var):
            host = value.strip()
        if host is None:
            continue
        local = host.lower() in _LOCAL_HOSTS or host in compose_services or host.startswith("/")
        out.append(DatabaseTarget(var=var, host=host or "local socket", local=local))
    return out


def compose_services(worktree: Path, file: str) -> set[str]:
    """Service names in a compose file (top-level `services:` keys), without a YAML parser."""
    p = worktree / file
    if not p.is_file():
        return set()
    names, inside, indent = set(), False, None
    for line in p.read_text(errors="replace").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if re.match(r"^services:\s*$", line):
            inside, indent = True, None
            continue
        if inside:
            if not line.startswith((" ", "\t")):
                break
            n = len(line) - len(line.lstrip())
            indent = indent or n
            if n == indent and (m := re.match(r"\s*([A-Za-z0-9._-]+):", line)):
                names.add(m.group(1))
    return names


def check_setup_guard(cfg: Config, slug: str, run: RunConfig, worktree: Path) -> None:
    """Raise SetupNeedsConfirm unless every database the setup commands would use is local."""
    if not run.setup:
        return
    env = {**os.environ, **file_env(worktree, run.env_files), **run.env}
    names = compose_services(worktree, run.compose.file) if run.compose else set()
    targets = database_targets(env, names)
    if targets and all(t.local for t in targets):
        return
    raise SetupNeedsConfirm([c.name for c in run.setup], targets)


# Starting

def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _project(pr: PullRequest) -> str:
    return re.sub(r"[^a-z0-9_-]", "-", f"reviewr-{pr.ref.owner}-{pr.ref.repo}-{pr.ref.number}".lower())


def _tail(path: Path, limit: int = LOG_TAIL) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - limit))
            return f.read().decode(errors="replace")
    except OSError:
        return ""


def read_log(cfg: Config, pr: PullRequest, service: str, limit: int = 40000) -> str:
    s = load(cfg, pr)
    svc = next((x for x in s.services if x.name == service), None) if s else None
    if not svc:
        raise RunError(f"No service named {service} in this run.")
    return _tail(Path(svc.log_path), limit)


def start(cfg: Config, pr: PullRequest, target: Target, setup: Literal["ask", "run", "skip"] = "ask") -> RunSession:
    """Start the app. Preparing the worktree, copying env files and the database guard
    happen here, so their errors come back to the caller; installs, compose, setup
    commands and services run in the background."""
    repo = cfg.repos.get(pr.ref.slug)
    run = repo.run if repo else None
    if not run or not run.services:
        raise RunError("There's no saved way to run this repository yet. Review the proposed commands and save them first.")
    key = _key(pr)
    with _lock(key):
        current = load(cfg, pr)
        if current and current.status in ("starting", "running", "stopping"):
            raise RunError("The app is already running for this PR. Stop it first.")
        busy = [svc for svc in run.services if svc.port and port_in_use(svc.port)]
        if busy:
            ports = ", ".join(f"{svc.port} ({svc.name})" for svc in busy)
            raise RunError(
                f"Port {ports} is already in use, so the app can't start there. Stop whatever is using it "
                f"(to see what: lsof -i :{busy[0].port}), or another PR's run of this repo."
            )
        worktree = _github_worktree(cfg, pr) if target == "github" else fixwork.work_worktree(cfg, pr)
        notes = copy_env_files(cfg, pr.ref.slug, worktree, run.env_files)
        if setup == "ask":
            check_setup_guard(cfg, pr.ref.slug, run, worktree)
        logs = runs_dir(cfg) / key
        logs.mkdir(parents=True, exist_ok=True)
        steps = [
            Step(id="worktree", title="Code to run", status="done", detail=str(worktree)),
            Step(id="deps", title="Dependencies"),
            Step(id="env_files", title="Env files", status="done" if run.env_files else "skipped",
                 detail="; ".join(notes)),
        ]
        if run.compose:
            steps.append(Step(id="compose", title=f"Docker Compose ({run.compose.file})"))
        for c in run.setup:
            steps.append(Step(id=f"setup:{c.name}", title=c.name, status="skipped" if setup == "skip" else "pending",
                              detail="Skipped for this run." if setup == "skip" else ""))
        s = RunSession(
            slug=pr.ref.slug, number=pr.ref.number, target=target, worktree=str(worktree),
            head_sha=git.head_sha(worktree), started_at=datetime.now(UTC), steps=steps,
            services=[ServiceState(name=svc.name, port=svc.port, open=svc.open, log_path=str(logs / f"{_safe(svc.name)}.log"),
                                   url=f"http://localhost:{svc.port}{svc.url_path}" if svc.port else None)
                      for svc in run.services],
            compose_project=_project(pr) if run.compose else None,
            compose_file=run.compose.file if run.compose else None,
            setup_skipped=setup == "skip",
        )
        _stop_requested.discard(key)
        _save(cfg, key, s)
        _jobs.add(key)
        _started[cfg.data_dir] = cfg
    threading.Thread(target=_start_job, args=(cfg, pr, run, s, setup != "skip"), name=f"run-{key}", daemon=True).start()
    return s


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


class _Session:
    """Updates one session from the background job, saving each change."""

    def __init__(self, cfg: Config, key: str, s: RunSession):
        self.cfg, self.key, self.s = cfg, key, s
        self._m = threading.Lock()

    def _save(self) -> None:
        with _lock(self.key):
            if not self.stopping():
                _save(self.cfg, self.key, self.s)

    def step(self, sid: str, status: StepStatus | None = None, detail: str | None = None, log_text: str | None = None) -> None:
        with self._m:
            st = next(x for x in self.s.steps if x.id == sid)
            if status is not None:
                st.status = status
            if detail is not None:
                st.detail = detail
            if log_text is not None:
                st.log = log_text[-LOG_TAIL:]
            self._save()

    def service(self, name: str, **changes) -> None:
        with self._m:
            svc = next(x for x in self.s.services if x.name == name)
            for k, v in changes.items():
                setattr(svc, k, v)
            self._save()

    def stopping(self) -> bool:
        return self.key in _stop_requested

    def finish(self, status, error: str | None = None) -> None:
        with self._m:
            self.s.status, self.s.error = status, error
            self._save()  # not if a stop came first; it saves its own state

    def popen(self, name: str, argv: list[str], cwd: Path, env: dict[str, str], stdout) -> subprocess.Popen | None:
        """Start a process in its own group and register it, unless a stop came first."""
        with _lock(self.key):
            if self.stopping():
                return None
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=stdout, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
            _procs[(self.key, name)] = proc
            return proc


def _start_job(cfg: Config, pr: PullRequest, run: RunConfig, s: RunSession, run_setup: bool) -> None:
    key = _key(pr)
    job = _Session(cfg, key, s)
    worktree = Path(s.worktree)
    env = process_env(cfg, pr.ref.slug, run)
    try:
        job.step("deps", "running")
        try:
            links = envs.link_envs(cfg, pr.ref.slug, worktree)
            s.links = [str(p) for p in links]
            job.step("deps", "done" if cfg.repos[pr.ref.slug].envs else "skipped",
                     f"{len(links)} linked from reviewr's installs" if links else "Nothing to link")
        except (envs.EnvError, OSError, subprocess.SubprocessError) as e:
            job.step("deps", "warning", f"Couldn't install or link dependencies; continuing: {e}")

        if run.compose and not job.stopping():
            job.step("compose", "running")
            if not shutil.which("docker"):
                raise RunError("Docker isn't installed or isn't on PATH, so the Compose services can't start.")
            argv = ["docker", "compose", "-p", s.compose_project, "-f", run.compose.file, "up", "-d", *run.compose.services]
            proc = subprocess.run(argv, cwd=worktree, env=env, capture_output=True, text=True, errors="replace", timeout=900,
                                  check=False)
            job.step("compose", "done" if proc.returncode == 0 else "failed", s.compose_project, proc.stdout + proc.stderr)
            if proc.returncode != 0:
                raise RunError("docker compose up failed; see its log. Is Docker running?")

        if run_setup:
            for c in run.setup:
                if job.stopping():
                    return
                sid = f"setup:{c.name}"
                job.step(sid, "running", " ".join(c.run))
                try:
                    proc = job.popen(sid, c.run, worktree / c.cwd, env, subprocess.PIPE)
                    if proc is None:
                        return
                    out, _ = proc.communicate(timeout=c.timeout)
                except subprocess.TimeoutExpired as e:
                    _kill_group(proc.pid, proc)
                    job.step(sid, "failed", f"Didn't finish within {c.timeout}s")
                    raise RunError(f"{c.name} didn't finish within {c.timeout} seconds.") from e
                except OSError as e:
                    job.step(sid, "failed", str(e))
                    raise RunError(f"{c.name} couldn't start: {e}") from e
                finally:
                    _procs.pop((key, sid), None)
                if job.stopping():
                    return
                job.step(sid, "done" if proc.returncode == 0 else "failed", None, out.decode(errors="replace"))
                if proc.returncode != 0:
                    raise RunError(f"{c.name} failed (exit {proc.returncode}); see its log. The services weren't started.")

        for svc in run.services:
            if job.stopping():
                return
            _start_service(job, key, svc, worktree, process_env(cfg, pr.ref.slug, run, svc.env))
        _wait_ready(job, run)
        job.finish("running")
    except Exception as e:  # shown on the page
        log.exception("Running the app for %s failed", key)
        job.finish("failed", str(e) or type(e).__name__)
    finally:
        _jobs.discard(key)


def _start_service(job: _Session, key: str, svc: ServiceConfig, worktree: Path, env: dict[str, str]) -> None:
    state = next(x for x in job.s.services if x.name == svc.name)
    # The process gets its own copy of the file descriptor, so closing ours is fine.
    with open(state.log_path, "ab") as out:
        out.write(f"\n--- {datetime.now(UTC).isoformat()} {' '.join(svc.run)} (in {svc.cwd})\n".encode())
        out.flush()
        try:
            proc = job.popen(svc.name, svc.run, worktree / svc.cwd, env, out)
        except OSError as e:
            job.service(svc.name, status="exited", note=f"Couldn't start: {e}")
            raise RunError(f"{svc.name} couldn't start: {e}") from e
    if proc is not None:
        job.service(svc.name, pid=proc.pid, status="starting")


def _wait_ready(job: _Session, run: RunConfig) -> None:
    """Wait until each service listens on its port (or, without one, is still up after
    a few seconds). A service that exits first fails the start."""
    deadline = time.monotonic() + run.ready_timeout
    pending = {svc.name: svc for svc in run.services}
    started = time.monotonic()
    while pending and not job.stopping():
        for name, svc in list(pending.items()):
            proc = _procs.get((job.key, name))
            if proc is not None and proc.poll() is not None:
                state = next(x for x in job.s.services if x.name == name)
                job.service(name, status="exited", exit_code=proc.returncode)
                raise RunError(f"{name} exited (code {proc.returncode}) before it was ready:\n\n{_tail(Path(state.log_path), 2000)}")
            if svc.port is None and time.monotonic() - started > 3 or svc.port and port_in_use(svc.port):
                job.service(name, status="running")
                del pending[name]
        if time.monotonic() > deadline:
            for name, svc in pending.items():
                job.service(name, status="running",
                            note=f"Not answering on port {svc.port} after {run.ready_timeout}s. It may still be starting; see its log.")
            return
        time.sleep(0.3)


# Stopping

def _kill_group(pid: int, proc: subprocess.Popen | None) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + STOP_GRACE_S
    while time.monotonic() < deadline:
        if (proc.poll() is not None) if proc is not None else not _alive(pid):
            break
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    if proc is not None:
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def stop(cfg: Config, pr: PullRequest, remove_data: bool = False) -> RunSession | None:
    """Stop the services and any setup command still running (process group: SIGTERM,
    then SIGKILL after a grace period), bring Compose down (with its volumes if
    `remove_data`), and undo the env links."""
    key = _key(pr)
    _stop_requested.add(key)  # the start job, if any, starts and saves nothing more
    try:
        with _lock(key):
            s = load(cfg, pr)
            if not s:
                return None
            return _stop(cfg, key, s, remove_data)
    finally:
        _stop_requested.discard(key)


def _stop(cfg: Config, key: str, s: RunSession, remove_data: bool = False) -> RunSession:
    s.status = "stopping"
    _save(cfg, key, s)
    pids = {svc.pid for svc in s.services if svc.pid}
    for (k, name), proc in list(_procs.items()):
        if k == key:
            _kill_group(proc.pid, proc)
            _procs.pop((k, name), None)
            pids.discard(proc.pid)
    for pid in pids:  # started before a restart of reviewr
        _kill_group(pid, None)
    for svc in s.services:
        if svc.status in ("starting", "running"):
            svc.status = "stopped"
    for st in s.steps:
        if st.status in ("pending", "running"):
            st.status = "skipped"
    if s.compose_project and s.compose_file and shutil.which("docker"):
        argv = ["docker", "compose", "-p", s.compose_project, "-f", s.compose_file, "down", *(["-v"] if remove_data else [])]
        try:
            proc = subprocess.run(argv, cwd=s.worktree, capture_output=True, text=True, errors="replace", timeout=300, check=False)
            if proc.returncode != 0:
                s.error = f"docker compose down failed: {(proc.stderr or proc.stdout).strip()[-1000:]}"
        except (OSError, subprocess.TimeoutExpired) as e:
            s.error = f"docker compose down failed: {e}"
    envs.unlink([Path(p) for p in s.links])
    s.links = []
    s.status = "stopped"
    _save(cfg, key, s)
    return s


def stop_all(cfg: Config | None = None) -> None:
    """Stop every run that's starting or running; called when the backend shuts down.
    Without `cfg`, only where this process started runs (so exiting a process that
    never ran anything reads no settings)."""
    for c in [cfg] if cfg else list(_started.values()):
        _stop_all_in(c)


def _stop_all_in(cfg: Config) -> None:
    if not runs_dir(cfg).is_dir():
        return
    for p in runs_dir(cfg).glob("*.json"):
        key = p.stem
        try:
            s = RunSession.model_validate_json(p.read_text())
        except (OSError, ValueError):
            continue
        if s.status in ("starting", "running", "stopping"):
            _stop_requested.add(key)
            try:
                with _lock(key):
                    _stop(cfg, key, s)
            except Exception:  # keep stopping the others
                log.exception("Stopping the run %s failed", key)
            finally:
                _stop_requested.discard(key)


def view(cfg: Config, pr: PullRequest) -> dict:
    s = load(cfg, pr)
    return {
        "session": s.model_dump(mode="json") if s else None,
        "head_moved": bool(s and s.target == "github" and s.status in ("starting", "running") and s.head_sha != pr.head_oid),
    }
