"""Running the PR's app: detection of how to run it."""

import json

from reviewr import detect
from reviewr.config import ComposeConfig


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
