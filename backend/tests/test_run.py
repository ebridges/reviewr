"""Running the PR's app: detection, the database guard, and starting/stopping services."""

import json
import os
import socket
import sys
import time

import pytest

from reviewr import detect, run
from reviewr.config import ComposeConfig, RunConfig, ServiceConfig, SetupCommand
from tests.test_merge import setup, sh  # noqa: F401  (setup is a fixture)


def reader(files: dict[str, str]):
    return lambda p: files[p].encode() if p in files else None


# Detection

def test_detect_prefers_launch_json():
    files = {
        ".claude/launch.json": json.dumps({"configurations": [
            {"name": "backend", "runtimeExecutable": "uv", "runtimeArgs": ["run", "serve"], "port": 8765},
            {"name": "frontend", "runtimeExecutable": "npm", "runtimeArgs": ["--prefix", "frontend", "run", "dev"], "port": 5173},
        ]}),
        "Procfile": "web: gunicorn app:app\n",
    }
    cfg, notes = detect.detect_run(list(files), reader(files))
    assert [(s.name, s.run, s.port) for s in cfg.services] == [
        ("backend", ["uv", "run", "serve"], 8765), ("frontend", ["npm", "--prefix", "frontend", "run", "dev"], 5173)]
    assert cfg.services[1].open and not cfg.services[0].open  # the front end is the one to open
    assert any("launch.json" in n for n in notes)


def test_detect_package_dev_script_with_port():
    files = {
        "web/package.json": json.dumps({"scripts": {"dev": "vite --port 4000", "db:migrate": "prisma migrate dev"}}),
        "web/pnpm-lock.yaml": "",
        "web/.env.example": "API=1\n",
    }
    cfg, _ = detect.detect_run(list(files), reader(files))
    (svc,) = cfg.services
    assert svc.run == ["pnpm", "--dir", "web", "run", "dev"] and svc.port == 4000 and svc.open
    assert cfg.env_files == ["web/.env"]
    assert [c.run for c in cfg.setup] == [["pnpm", "--dir", "web", "run", "db:migrate"]]


def test_detect_procfile_compose_and_migrations():
    files = {
        "Procfile": "release: alembic upgrade head\nweb: uvicorn app:app --port 8000\nworker: celery -A app worker\n",
        "docker-compose.yml": "services:\n  db:\n    image: postgres\n",
        "alembic.ini": "",
        ".gitignore": ".env\nnode_modules\n",
    }
    cfg, notes = detect.detect_run(list(files), reader(files))
    assert [(s.name, s.port) for s in cfg.services] == [("web", 8000), ("worker", None)]
    assert cfg.compose == ComposeConfig(file="docker-compose.yml")
    assert cfg.setup[0].run == [".venv/bin/alembic", "upgrade", "head"]
    assert cfg.env_files == [".env"]
    assert any("worker: no port" in n for n in notes)


# The database guard

def test_database_targets():
    env = {
        "DATABASE_URL": "postgresql://u:p@db.staging.internal:5432/app",
        "CACHE_URL": "redis://localhost:6379/0",
        "HOME_URL": "https://example.com",  # not a database
        "DB_HOST": "db",
        "PGHOST": "/var/run/postgresql",
        "TEST_DATABASE_URL": "sqlite:///tmp/x.db",
    }
    got = {t.var: (t.host, t.local) for t in run.database_targets(env, {"db"})}
    assert got == {
        "DATABASE_URL": ("db.staging.internal", False), "CACHE_URL": ("localhost", True), "DB_HOST": ("db", True),
        "PGHOST": ("/var/run/postgresql", True), "TEST_DATABASE_URL": ("sqlite file", True),
    }


def test_guard_uses_env_overrides_over_env_files(setup, monkeypatch):  # noqa: F811
    cfg, pr, clone = setup
    for var in [v for v in os.environ if "DATABASE" in v or v.endswith("_HOST")]:
        monkeypatch.delenv(var)
    (clone / ".env").write_text('DATABASE_URL="postgres://u:p@shared-db.example.com/app"  # the team database\n')
    rc = RunConfig(env_files=[".env"], setup=[SetupCommand(name="migrate", run=["true"])])
    with pytest.raises(run.SetupNeedsConfirm) as e:
        run.check_setup_guard(cfg, pr.ref.slug, rc, clone)
    assert e.value.commands == ["migrate"] and "shared-db.example.com" in str(e.value)
    rc.env = {"DATABASE_URL": "postgres://u:p@localhost:55432/app"}  # the PR's own database
    run.check_setup_guard(cfg, pr.ref.slug, rc, clone)  # no error
    with pytest.raises(run.SetupNeedsConfirm, match="couldn't tell"):
        run.check_setup_guard(cfg, pr.ref.slug, RunConfig(setup=rc.setup), clone)


def test_parse_env_file():
    text = "# c\nexport A=1\nB='two words'\nC=3 # note\nbad line\nD=\"x#y\"\n"
    assert run.parse_env_file(text) == {"A": "1", "B": "two words", "C": "3", "D": "x#y"}


# Starting and stopping

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait(cfg, pr, until, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        s = run.load(cfg, pr)
        if until(s):
            return s
        time.sleep(0.1)
    raise AssertionError(f"timed out; last state: {s}")


def web(port: int, name: str = "web") -> ServiceConfig:
    return ServiceConfig(name=name, run=[sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"], port=port, open=True)


@pytest.fixture
def app(setup):  # noqa: F811
    cfg, pr, clone = setup
    yield cfg, pr, clone
    run.stop(cfg, pr)  # never leave a server behind


def test_service_starts_and_stops(app):
    cfg, pr, _clone = app
    port = free_port()
    cfg.repos[pr.ref.slug].run = RunConfig(services=[web(port)])
    run.start(cfg, pr, "github")
    s = wait(cfg, pr, lambda s: s.status != "starting")
    assert s.status == "running" and s.services[0].status == "running"
    assert s.services[0].url == f"http://localhost:{port}/" and run.port_in_use(port)
    pid = s.services[0].pid
    with pytest.raises(run.RunError, match="already running"):
        run.start(cfg, pr, "github")
    s = run.stop(cfg, pr)
    assert s.status == "stopped" and s.services[0].status == "stopped"
    assert not run.port_in_use(port)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert "Serving HTTP" in run.read_log(cfg, pr, "web") or run.read_log(cfg, pr, "web")  # log file written


def test_service_that_exits_fails_the_start(app):
    cfg, pr, _clone = app
    cfg.repos[pr.ref.slug].run = RunConfig(services=[
        ServiceConfig(name="web", run=[sys.executable, "-c", "print('boom: missing SECRET'); raise SystemExit(3)"], port=free_port())])
    run.start(cfg, pr, "github")
    s = wait(cfg, pr, lambda s: s.status != "starting")
    assert s.status == "failed" and "code 3" in s.error and "boom: missing SECRET" in s.error
    assert s.services[0].status == "exited"


def test_port_in_use_is_refused(app):
    cfg, pr, _clone = app
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        cfg.repos[pr.ref.slug].run = RunConfig(services=[web(port)])
        with pytest.raises(run.RunError, match=f"Port {port}"):
            run.start(cfg, pr, "github")
    assert run.load(cfg, pr) is None  # nothing started


def test_targets_and_env_files(app):
    cfg, pr, clone = app
    (clone / ".git" / "info" / "exclude").write_text(".env\n")
    (clone / ".env").write_text("SECRET=1\n")
    (clone / "local.cfg").write_text("x\n")  # not ignored: never copied
    (clone / "app.py").write_text((clone / "app.py").read_text() + "MINE = 1\n")  # your uncommitted edit
    cfg.repos[pr.ref.slug].run = RunConfig(env_files=[".env", "local.cfg"], services=[web(free_port())])

    s = run.start(cfg, pr, "github")
    wt = run.run_root(cfg, pr)
    assert s.worktree == str(wt) and "MINE" not in (wt / "app.py").read_text()
    assert (wt / ".env").read_text() == "SECRET=1\n" and not (wt / "local.cfg").exists()
    notes = next(st.detail for st in s.steps if st.id == "env_files")
    assert ".env: copied" in notes and "local.cfg: git doesn't ignore it" in notes
    wait(cfg, pr, lambda s: s.status != "starting")
    run.stop(cfg, pr)

    (wt / ".env").write_text("SECRET=edited in the run worktree\n")
    run.start(cfg, pr, "github")  # again: the worktree is reused, and the env file isn't overwritten
    assert (wt / ".env").read_text() == "SECRET=edited in the run worktree\n"
    wait(cfg, pr, lambda s: s.status != "starting")
    run.stop(cfg, pr)

    s = run.start(cfg, pr, "checkout")
    assert s.worktree == str(clone) and "MINE" in (clone / "app.py").read_text()


def test_setup_commands_and_the_guard(app, monkeypatch):
    cfg, pr, _clone = app
    for var in [v for v in os.environ if "DATABASE" in v or v.endswith("_HOST")]:
        monkeypatch.delenv(var)
    migrate = SetupCommand(name="migrate", run=[sys.executable, "-c", "open('migrated', 'w').write('ok')"])
    cfg.repos[pr.ref.slug].run = RunConfig(setup=[migrate], services=[web(free_port())])
    with pytest.raises(run.SetupNeedsConfirm):
        run.start(cfg, pr, "github")
    assert run.load(cfg, pr) is None

    run.start(cfg, pr, "github", setup="skip")
    s = wait(cfg, pr, lambda s: s.status != "starting")
    assert s.setup_skipped and not (run.run_root(cfg, pr) / "migrated").exists()
    run.stop(cfg, pr)

    run.start(cfg, pr, "github", setup="run")
    s = wait(cfg, pr, lambda s: s.status != "starting")
    assert s.status == "running" and (run.run_root(cfg, pr) / "migrated").read_text() == "ok"
    assert next(st for st in s.steps if st.id == "setup:migrate").status == "done"


def test_failed_setup_command_stops_the_start(app):
    cfg, pr, _clone = app
    bad = SetupCommand(name="migrate", run=[sys.executable, "-c", "print('relation exists'); raise SystemExit(1)"])
    cfg.repos[pr.ref.slug].run = RunConfig(setup=[bad], services=[web(free_port())])
    run.start(cfg, pr, "github", setup="run")
    s = wait(cfg, pr, lambda s: s.status != "starting")
    assert s.status == "failed" and "migrate failed" in s.error
    step = next(st for st in s.steps if st.id == "setup:migrate")
    assert step.status == "failed" and "relation exists" in step.log
    assert s.services[0].pid is None  # services never started


def test_running_service_survives_a_reviewr_restart(app):
    cfg, pr, _clone = app
    cfg.repos[pr.ref.slug].run = RunConfig(services=[web(free_port())])
    run.start(cfg, pr, "github")
    s = wait(cfg, pr, lambda s: s.status != "starting")
    pid = s.services[0].pid
    procs = dict(run._procs)
    run._procs.clear()  # what a new reviewr process knows: only the saved session
    try:
        assert run.load(cfg, pr).services[0].status == "running"
        run.stop(cfg, pr)  # stops it by process group
        # It's this test's child, so it stays a zombie until collected; after a real
        # restart the OS collects it. Either way it has exited.
        (proc,) = [p for p in procs.values() if p.pid == pid]
        assert proc.wait(timeout=5) is not None
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.kill()
            p.wait()


def test_stop_all_stops_only_what_this_process_started(app):
    cfg, pr, _clone = app
    cfg.repos[pr.ref.slug].run = RunConfig(services=[web(free_port())])
    run.start(cfg, pr, "github")
    wait(cfg, pr, lambda s: s.status != "starting")
    run.stop_all()
    assert run.load(cfg, pr).status == "stopped"
