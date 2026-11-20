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


def _hook(clone: Path, script: str) -> None:
    hook = clone / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\n" + script)
    hook.chmod(0o755)


def _resolve_all(cfg, pr):
    """Take main's side (or the deletion) everywhere, so app.py differs from the branch."""
    merge.update(cfg, pr, lambda s: [merge.resolve(c, merge.options_for(c)[1]) for c in s.conflicts.values()])


def test_commit_keeps_formatting_from_hooks(setup):
    """A formatter hook that fails after changing files (as pre-commit does): the changes
    are staged and the commit is retried once."""
    cfg, pr, clone = setup
    _hook(clone, """changed=0
for f in $(git diff --cached --name-only --diff-filter=AM); do
  if grep -q "'" "$f"; then sed -i.bak "s/'/\\"/g" "$f" && rm "$f.bak"; changed=1; fi
done
exit $changed
""")
    merge.start(cfg, pr, no_ai)
    _resolve_all(cfg, pr)
    s = merge.commit(cfg, pr, "Merge main")
    assert s.status == "committed" and "app.py" in s.hook_fixes
    assert "'" not in (clone / "app.py").read_text()
    assert sh(clone, "rev-parse", "HEAD").strip() == s.commit_sha


def test_commit_reports_failing_hooks(setup):
    cfg, pr, clone = setup
    head = sh(clone, "rev-parse", "HEAD").strip()
    _hook(clone, "echo 'lint: 3 problems' >&2\nexit 1\n")
    merge.start(cfg, pr, no_ai)
    _resolve_all(cfg, pr)
    with pytest.raises(merge.MergeError, match="(?s)commit hooks failed.*lint: 3 problems"):
        merge.commit(cfg, pr, "Merge main")
    assert merge.load(cfg, pr).status != "committed"
    assert sh(clone, "rev-parse", "HEAD").strip() == head  # your checkout didn't move


def _wait(cfg, pr, done):
    for _ in range(200):
        s = merge.load(cfg, pr)
        if done(s):
            return s
        time.sleep(0.05)
    raise AssertionError("background job didn't finish")


def _fake_agent(monkeypatch, edit):
    import reviewr.agent

    def run(worktree, prompt, model):
        assert "lint: bad quotes" in prompt and "app.py" in prompt
        edit(Path(worktree))
        return "Changed the quotes in app.py."

    monkeypatch.setattr(reviewr.agent, "run_agent", run)


def test_fix_applied_is_committed(setup, monkeypatch):
    cfg, pr, clone = setup
    merge.start(cfg, pr, no_ai)
    _resolve_all(cfg, pr)
    _fake_agent(monkeypatch, lambda t: (t / "app.py").write_text((t / "app.py").read_text().replace("'", '"')))
    merge.start_fix(cfg, pr, "lint: bad quotes in app.py", "Claude")
    s = _wait(cfg, pr, lambda s: s.fix.status != "running")
    assert s.fix.status == "ready" and "app.py" in s.fix.diff and s.fix.summary.startswith("Changed")
    with pytest.raises(merge.MergeError, match="Apply or discard"):
        merge.commit(cfg, pr, "m")
    merge.apply_fix(cfg, pr)
    s = merge.commit(cfg, pr, "Merge main")
    assert "'" not in (clone / "app.py").read_text()  # the fix survived rewriting resolved files


def test_fix_discarded_restores_merge(setup, monkeypatch):
    cfg, pr, _clone = setup
    merge.start(cfg, pr, no_ai)
    _resolve_all(cfg, pr)
    tree = merge.tree_path(cfg, pr)
    _fake_agent(monkeypatch, lambda t: ((t / "app.py").write_text("broken\n"), (t / "extra.py").write_text("x\n")))
    merge.start_fix(cfg, pr, "lint: bad quotes in app.py", "Codex")
    s = _wait(cfg, pr, lambda s: s.fix.status != "running")
    assert "extra.py" in s.fix.diff
    merge.discard_fix(cfg, pr)
    resolved = sh(tree, "show", ":app.py")  # the staged resolution
    assert (tree / "app.py").read_text() == resolved and "broken" not in resolved and not (tree / "extra.py").exists()
    assert merge.load(cfg, pr).fix is None


def test_own_fix_proposed_then_applied(setup, monkeypatch, tmp_path):
    """Fix page, your own changes: the agent works on a copy; your checkout changes only on Apply."""
    from reviewr import fixwork
    from reviewr.state import ProposedFix, WorkItem
    from reviewr.store import Store

    cfg, pr, clone = setup
    st = Store(tmp_path / "s.sqlite3")
    st.update_state(pr.ref, lambda s: s.plan.append(WorkItem(id=1, title="Say hello", thread_ids=[], mode="me")))
    write(clone, {"app.py": (clone / "app.py").read_text() + "MSG = 'hi'\n"})  # your uncommitted edit
    head = sh(clone, "rev-parse", "HEAD").strip()

    import reviewr.agent

    def agent(worktree, prompt, model):
        assert "lint: quotes" in prompt and "Say hello" in prompt
        p = Path(worktree) / "app.py"
        assert "MSG = 'hi'" in p.read_text()  # your edit is there for the agent
        p.write_text(p.read_text().replace("MSG = 'hi'", 'MSG = "hi"'))
        return "Used double quotes."

    monkeypatch.setattr(reviewr.agent, "run_agent", agent)
    monkeypatch.setattr(fixwork, "run_agent", agent)
    fixwork.start_own_fix(st, cfg, pr, 1, "lint: quotes in app.py", "Claude")
    for _ in range(200):
        f = st.get_state(pr.ref).item(1).proposed_fix
        if f.status != "running":
            break
        time.sleep(0.05)
    assert f.status == "ready" and "+MSG = \"hi\"" in f.diff and "-MSG = 'hi'" in f.diff
    assert "MSG = 'hi'" in (clone / "app.py").read_text()  # untouched until you apply

    fixwork.apply_own_fix(st, cfg, pr, 1)
    assert 'MSG = "hi"' in (clone / "app.py").read_text()
    assert sh(clone, "rev-parse", "HEAD").strip() == head  # applied as uncommitted edits
    assert st.get_state(pr.ref).item(1).proposed_fix is None

    # A proposal that no longer applies is refused, and your file is left alone.
    stale = ProposedFix(status="ready", failure="x", ai_name="Claude", diff=f.diff)  # already applied once
    st.update_state(pr.ref, lambda s: setattr(s.item(1), "proposed_fix", stale))
    with pytest.raises(ValueError, match="no longer applies"):
        fixwork.apply_own_fix(st, cfg, pr, 1)
    assert 'MSG = "hi"' in (clone / "app.py").read_text()


def _push_to_origin_branch(origin: Path, files: dict[str, str], msg: str) -> None:
    """Someone else adds a commit to the PR branch on GitHub."""
    sh(origin, "checkout", "-q", "feature")
    write(origin, files)
    sh(origin, "add", "-A")
    sh(origin, "commit", "-qm", msg)
    sh(origin, "checkout", "-q", "main")


def test_update_waits_for_github_commits_then_fast_forwards(setup):
    cfg, pr, clone = setup
    origin = clone.parent / "origin"
    _push_to_origin_branch(origin, {"other.py": "o = 1\n"}, "Update branch on GitHub")
    with pytest.raises(merge.MergeError, match="GitHub has 1 commit"):
        merge.start(cfg, pr, no_ai)
    st = merge.remote_status(cfg, pr)
    assert (st["behind"], st["ahead"]) == (1, 0)
    st = merge.sync_with_remote(cfg, pr)
    assert st["behind"] == 0 and (clone / "other.py").exists()
    assert sh(clone, "rev-parse", "HEAD") == sh(clone, "rev-parse", "origin/feature")  # fast-forward
    merge.start(cfg, pr, no_ai)  # now allowed


def test_sync_merges_when_both_sides_have_commits(setup):
    cfg, pr, clone = setup
    origin = clone.parent / "origin"
    _push_to_origin_branch(origin, {"other.py": "o = 1\n"}, "on GitHub")
    write(clone, {"mine.py": "m = 1\n"})
    sh(clone, "add", "mine.py")
    sh(clone, "commit", "-qm", "local work")
    st = merge.sync_with_remote(cfg, pr)
    assert (st["behind"], st["ahead"]) == (0, 2)  # local work + the merge commit
    assert (clone / "other.py").exists() and (clone / "mine.py").exists()


def test_sync_refuses_conflicts(setup):
    cfg, pr, clone = setup
    origin = clone.parent / "origin"
    _push_to_origin_branch(origin, {"same.py": "a = 'github'\n"}, "on GitHub")
    write(clone, {"same.py": "a = 'local'\n"})
    sh(clone, "commit", "-qam", "local work")
    head = sh(clone, "rev-parse", "HEAD")
    assert merge.remote_status(cfg, pr)["conflicted_files"] == ["same.py"]
    with pytest.raises(merge.MergeError, match="would conflict in: same.py"):
        merge.sync_with_remote(cfg, pr)
    assert sh(clone, "rev-parse", "HEAD") == head


def test_sync_works_offline_from_last_fetch(setup):
    """GitHub unreachable: the commits fetched earlier are merged anyway, and the result says so."""
    cfg, pr, clone = setup
    origin = clone.parent / "origin"
    _push_to_origin_branch(origin, {"other.py": "o = 1\n"}, "on GitHub")
    assert merge.remote_status(cfg, pr)["behind"] == 1  # fetched while reachable
    origin.rename(clone.parent / "origin-gone")  # now unreachable
    st = merge.sync_with_remote(cfg, pr)
    assert st["behind"] == 0 and st["fetch_error"] and (clone / "other.py").exists()


def test_agent_prompt_is_readable_and_editable(setup):
    """The Fix page shows this prompt; an edited one replaces it when drafting."""
    from reviewr.agent import agent_prompt, prompt_for
    from reviewr.models import Comment, Thread
    from reviewr.state import ReviewState, ThreadDecision, WorkItem

    _cfg, pr, _clone = setup
    body = ('**![P1 Badge](https://img.shields.io/x) Keep the margins** <sub>bot</sub>\n\nUse the base margin.'
            "\n\n<!-- hidden -->\nUseful? React with 👍 / 👎.")
    t = Thread(id="T", path="app.py", line=4, original_line=4, start_line=None, is_resolved=False, is_outdated=False,
               original_commit=None, diff_hunk="@@ -1,2 +1,3 @@\n def greet(name):\n+    x = 1",
               comments=[Comment(id="c", author="rev", body=body, created_at=datetime.now(UTC), url="u")])
    pr = pr.model_copy(update={"threads": [t]})
    state = ReviewState(decisions={"T": ThreadDecision(decision="fix", note="only in app.py", decided_at=datetime.now(UTC))})
    item = WorkItem(id=1, title="Keep margins", thread_ids=["T"])
    text = agent_prompt(pr, state, item)
    assert "**P1 Keep the margins**" in text and "<sub>" not in text and "img.shields" not in text
    assert "Useful?" not in text and "hidden" not in text
    assert "+    x = 1" in text and "only in app.py" in text
    assert prompt_for(pr, state, item) == text
    item.prompt = "my own prompt"
    assert prompt_for(pr, state, item) == "my own prompt"


def test_interrupted_draft_is_marked_failed(setup, tmp_path):
    """A draft left "drafting" by a restart doesn't wait forever."""
    from reviewr import fixwork
    from reviewr.state import WorkItem
    from reviewr.store import Store

    _cfg, pr, _clone = setup
    st = Store(tmp_path / "s.sqlite3")
    st.update_state(pr.ref, lambda s: s.plan.append(WorkItem(id=1, title="x", thread_ids=[], status="drafting", prompt="mine")))
    fixwork.recover_interrupted(st, pr)
    item = st.get_state(pr.ref).item(1)
    assert item.status == "failed" and "restarted" in item.error and item.prompt == "mine"


def test_merge_of_main_counts_as_one_commit_to_push(setup):
    """Respond's "commits to push": a merge counts once, not as every commit from main."""
    from reviewr import git as g

    cfg, pr, clone = setup
    merge.start(cfg, pr, no_ai)
    merge.update(cfg, pr, lambda s: [merge.resolve(c, merge.options_for(c)[0]) for c in s.conflicts.values()])
    merge.commit(cfg, pr, "Merge main")
    assert int(sh(clone, "rev-list", "--count", "origin/feature..HEAD")) == 2  # the merge and main's commit
    assert g.commits_ahead(clone, "origin/feature") == 1
