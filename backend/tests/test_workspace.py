import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from reviewr import detect, toolchains, workspace
from reviewr.config import Config, load_config, save_config
from reviewr.models import PRRef, PullRequest


def _reader(files: dict[str, str]):
    return lambda p: files[p].encode() if p in files else None


# Detection

def test_detects_installs_for_each_lockfile():
    files = {
        "package.json": json.dumps({"devDependencies": {"eslint": "9", "jest": "29"}}),
        "package-lock.json": "{}",
        ".nvmrc": "22",
        "web/package.json": json.dumps({"devDependencies": {"vitest": "2"}}),
        "web/pnpm-lock.yaml": "",
        "api/pyproject.toml": "[project]\nname='api'\n[tool.ruff]\n",
        "api/uv.lock": "pytest ruff",
        "scripts/requirements.txt": "black\n",
        "node_modules/x/package-lock.json": "{}",  # ignored
    }
    envs, notes = detect.detect_envs(list(files), _reader(files), python="3.12.4")
    by_name = {e.name: e for e in envs}
    assert set(by_name) == {"node", "node (web)", "python (api)", "python (scripts)"}
    assert by_name["node"].install == [["npm", "ci", "--prefix", ".", "--no-audit", "--no-fund"]]
    assert ".nvmrc" in by_name["node"].files and by_name["node"].seed == ["node_modules"]
    assert by_name["node (web)"].install == [["pnpm", "install", "--frozen-lockfile", "--dir", "web"]]
    assert by_name["python (api)"].install[0][:4] == ["uv", "sync", "--frozen", "--no-install-project"]
    assert "--python" in by_name["python (api)"].install[0]
    assert by_name["python (scripts)"].provides == {"scripts/.venv": "scripts/.venv"}
    assert notes == []

    checks = {c.name: c for c in detect.detect_checks(envs, list(files), _reader(files))}
    assert set(checks) == {"ESLint", "Jest", "Vitest (related tests) (web)", "Ruff (api)", "pytest (api)", "Black (scripts)"}
    assert checks["Ruff (api)"].cwd == "api" and checks["Ruff (api)"].match == ["api/*.py"]
    assert checks["pytest (api)"].discover == "python"


def test_yarn_plug_n_play_is_skipped_with_a_note():
    files = {"package.json": "{}", "yarn.lock": "", ".yarnrc.yml": "yarnPath: .yarn/releases/yarn.cjs\n"}
    envs, notes = detect.detect_envs(list(files), _reader(files))
    assert envs == [] and "Plug'n'Play" in notes[0]


def test_node_version_matching():
    assert toolchains.node_matches("22", (22, 11, 0))
    assert not toolchains.node_matches("22", (20, 1, 0))
    assert toolchains.node_matches(">=18 <21", (20, 0, 0))
    assert not toolchains.node_matches(">=18 <21", (22, 0, 0))
    assert toolchains.node_matches("^20.10.0", (20, 18, 1))
    assert toolchains.node_matches("lts/*", (22, 1, 0)) and not toolchains.node_matches("lts/*", (23, 1, 0))
    req, src = toolchains.node_request(_reader({"package.json": json.dumps({"engines": {"node": ">=20"}})}))
    assert (req, src) == (">=20", "package.json engines")
    req, src = toolchains.python_request(_reader({"pyproject.toml": "[project]\nrequires-python = '>=3.11'\n"}))
    assert (req, src) == (">=3.11", "pyproject.toml requires-python")


# Finding a clone and running setup

def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def _clone(path: Path, slug: str) -> tuple[str, str]:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    _git(path, "remote", "add", "origin", f"git@github.com:{slug}.git")
    (path / "README.md").write_text("hi\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "base")
    base = _git(path, "rev-parse", "HEAD")
    _git(path, "checkout", "-q", "-b", "feature")
    (path / "app.py").write_text("x = 1\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "feature")
    head = _git(path, "rev-parse", "HEAD")
    _git(path, "checkout", "-q", "main")
    return base, head


def test_find_clone_matches_the_remote_not_the_folder_name(tmp_path: Path):
    _clone(tmp_path / "work" / "misc" / "renamed", "acme/app")
    assert workspace.find_clone(tmp_path, "acme/app") == tmp_path / "work" / "misc" / "renamed"
    assert workspace.find_clone(tmp_path, "acme/other") is None


def test_setup_uses_an_existing_clone_and_checks_out_the_branch(tmp_path: Path):
    clone = tmp_path / "Projects" / "acme" / "app"
    base, head = _clone(clone, "acme/app")
    save_config(Config(workspace_dir=tmp_path / "Projects", data_dir=tmp_path / "data"))
    cfg = load_config()
    assert workspace.needed(cfg, "acme/app")
    pr = PullRequest(ref=PRRef(owner="acme", repo="app", number=7), node_id="n", title="t", url="u", author="a",
                     base_ref="main", head_ref="feature", base_oid=base, head_oid=head, threads=[], reviews=[],
                     issue_comments=[], fetched_at=datetime.now(UTC))
    runner = workspace.Runner(cfg, pr)
    runner.run()
    s = runner.s
    assert s.status == "done", s
    assert {st.id: st.status for st in s.steps}["clone"] == "done"
    cfg = load_config()
    assert cfg.repos["acme/app"].path == clone
    assert workspace.needed(cfg, "acme/app") is None
    assert _git(clone, "branch", "--show-current") == "main"  # your checkout is left alone
    assert "feature" in _git(clone, "worktree", "list")  # the PR branch got a worktree
    assert workspace.load(cfg, "acme/app").status == "done"
