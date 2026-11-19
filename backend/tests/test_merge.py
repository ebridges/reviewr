"""Updating a PR branch from main: conflicts, rules, resolutions, commit and fast-forward."""

import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reviewr import merge
from reviewr.config import Config, RepoConfig
from reviewr.models import PRRef, PullRequest

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=ENV).stdout


def write(root: Path, files: dict[str, str | None]) -> None:
    for name, text in files.items():
        p = root / name
        if text is None:
            p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)


@pytest.fixture
def setup(tmp_path):
    """origin with main and a PR branch that conflict in several ways; a clone with the
    PR branch checked out."""
    origin = tmp_path / "origin"
    origin.mkdir()
    sh(origin, "init", "-q", "-b", "main")
    write(origin, {
        "app.py": "import os\n\ndef greet(name):\n    return 'hi ' + name\n\nX = 1\n",
        "same.py": "a = 1\n",
        "ws.py": "def f():\n    return 1\n",
        "gone.py": "keep = True\n",
        "package-lock.json": '{"v": 1}\n',
    })
    sh(origin, "add", "-A")
    sh(origin, "commit", "-qm", "init")
    sh(origin, "checkout", "-qb", "feature")
    write(origin, {
        "app.py": "import os\nimport sys\n\ndef greet(name):\n    return 'hello ' + name\n\nX = 1\n",
        "same.py": "a = 2\n",
        "ws.py": "def f():\n        return 1\n",
        "gone.py": "keep = 'pr'\n",
        "package-lock.json": '{"v": 2}\n',
    })
    sh(origin, "commit", "-qam", "PR changes")
    sh(origin, "checkout", "-q", "main")
    write(origin, {
        "app.py": "import os\nimport json\n\ndef greet(name):\n    return 'hey ' + name\n\nX = 1\n",
        "same.py": "a = 2\n",
        "ws.py": "def f():\n    return 2\n",
        "gone.py": None,
        "package-lock.json": '{"v": 3}\n',
        "new.py": "n = 1\n",
    })
    sh(origin, "add", "-A")
    sh(origin, "commit", "-qm", "main changes")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, env=ENV)
    sh(clone, "checkout", "-q", "feature")
    head = sh(clone, "rev-parse", "HEAD").strip()

    cfg = Config(data_dir=tmp_path / "data", repos={"o/r": RepoConfig(path=clone)})
    pr = PullRequest(
        ref=PRRef(owner="o", repo="r", number=1), node_id="n", title="t", url="u", author="me",
        base_ref="main", head_ref="feature", base_oid="", head_oid=head, threads=[], reviews=[],
        issue_comments=[], fetched_at=datetime.now(UTC),
    )
    return cfg, pr, clone


def no_ai(s, conflicts):
    return {c.id: {"category": "input", "recommended": "ours", "title": "x", "reason": "y", "source": "ai"} for c in conflicts}


def test_parse_conflicts_diff3():
    text = "a\n<<<<<<< HEAD\nours\n||||||| base\nbase\n=======\ntheirs\n>>>>>>> main\nz\n"
    segments, found = merge.parse_conflicts(text)
    assert [s.text for s in segments] == ["a\n", None, "z\n"]
    assert found == [("ours\n", "base\n", "theirs\n")]


def test_preview_lists_conflicts_without_touching_checkout(setup):
    cfg, pr, clone = setup
    info = merge.preview(cfg, pr)
    assert info["behind"] == 1
    assert set(info["conflicted_files"]) == {"app.py", "ws.py", "gone.py", "package-lock.json"}
    assert sh(clone, "status", "--porcelain") == ""


def test_start_classifies_resolves_and_commits(setup):
    cfg, pr, clone = setup
    s = merge.start(cfg, pr, no_ai)
    from reviewr import fixwork
    # With the clone on another branch, the merge tree is the only worktree detached at the
    # PR head. It's reviewr's, so it must not be taken for your checkout.
    sh(clone, "checkout", "-q", "main")
    assert fixwork.existing_work_tree(cfg, pr) is None
    sh(clone, "checkout", "-q", "feature")
    kinds = {c.path: c.kind for c in s.conflicts.values()}
    assert kinds == {"app.py": "text", "ws.py": "text", "gone.py": "deleted_in_main", "package-lock.json": "lockfile"}
    assert "new.py" in s.clean_files and "same.py" not in s.clean_files  # same.py: both sides identical, git merged it

    ws = next(c for c in s.conflicts.values() if c.path == "ws.py")
    assert (ws.category, ws.recommended, ws.source) == ("auto", "theirs", "rule")  # PR only re-indented

    # Wait for the background "AI" to triage app.py's two conflicts.
    for _ in range(100):
        s = merge.load(cfg, pr)
        if s.status == "ready":
            break
        time.sleep(0.05)
    app = sorted((c for c in s.conflicts.values() if c.path == "app.py"), key=lambda c: c.index)
    assert [c.category for c in app] == ["input", "input"]
    assert app[0].ours == "import sys\n" and app[0].theirs == "import json\n"

    with pytest.raises(merge.MergeError):
        merge.commit(cfg, pr, "merge")  # unresolved conflicts

    def resolve_all(s):
        for c in s.conflicts.values():
            if c.path == "app.py" and c.index == 0:
                merge.resolve(c, "ours_then_theirs")
            elif c.path == "app.py":
                merge.resolve(c, "custom", "    return 'hey there ' + name\n")
            elif c.path == "gone.py":
                merge.resolve(c, "delete")
            else:
                merge.resolve(c, c.recommended)

    merge.update(cfg, pr, resolve_all)
    s = merge.commit(cfg, pr, "Merge main into feature")
    assert s.status == "committed"
    # The user's checkout was fast-forwarded to the merge commit.
    assert sh(clone, "rev-parse", "HEAD").strip() == s.commit_sha
    assert (clone / "app.py").read_text() == "import os\nimport sys\nimport json\n\ndef greet(name):\n    return 'hey there ' + name\n\nX = 1\n"
    assert (clone / "ws.py").read_text() == "def f():\n    return 2\n"
    assert not (clone / "gone.py").exists()
    assert (clone / "package-lock.json").read_text() == '{"v": 3}\n'
    assert (clone / "new.py").exists()
    assert len(sh(clone, "log", "-1", "--format=%P").split()) == 2  # a real merge commit


def test_commit_refuses_if_checkout_moved(setup):
    cfg, pr, clone = setup
    merge.start(cfg, pr, no_ai)
    write(clone, {"other.py": "o = 1\n"})
    sh(clone, "add", "other.py")
    sh(clone, "commit", "-qm", "later work")
    merge.update(cfg, pr, lambda s: [merge.resolve(c, merge.options_for(c)[0]) for c in s.conflicts.values()])
    with pytest.raises(merge.MergeError, match="moved"):
        merge.commit(cfg, pr, "m")
    merge.discard(cfg, pr)
    assert merge.load(cfg, pr) is None
    assert not merge.tree_path(cfg, pr).exists()


def test_custom_resolution_rejects_markers():
    c = merge.Conflict(id="x", path="p", index=0, kind="text", ours="a\n", base="", theirs="b\n")
    with pytest.raises(merge.MergeError):
        merge.resolve(c, "custom", "<<<<<<< HEAD\na\n")
    assert merge.option_text(c, "theirs_then_ours") == "b\na\n"


def test_check_skips_when_too_many_tests(tmp_path):
    from reviewr.checks import ChangedPath, _one
    from reviewr.config import CheckConfig

    (tmp_path / "tests").mkdir()
    for i in range(5):
        (tmp_path / "tests" / f"t{i}.test.js").write_text("")
    listing = [f"tests/t{i}.test.js" for i in range(5)]
    check = CheckConfig(name="jest", kind="test", match=["*.js"], run=["false"], max_tests=3,
                        discover=["printf", "\\n".join(listing)])
    run = _one(check, tmp_path, tmp_path, RepoConfig(path=tmp_path), [ChangedPath(path="a.js")])
    assert run.status == "skipped" and "limit of 3" in run.reason and len(run.tests) == 5
