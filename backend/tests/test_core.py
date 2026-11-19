import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from reviewr import git, submit
from reviewr.github.client import GitHubClient
from reviewr.github.fetch import fetch_pull_request
from reviewr.models import PRRef
from reviewr.state import ReviewState, ThreadDecision, WorkItem


def test_parse_pr_ref():
    assert PRRef.parse("https://github.com/a/b/pull/12/files").key == "a/b#12"
    assert PRRef.parse("a/b#7").number == 7
    with pytest.raises(ValueError):
        PRRef.parse("not a pr")


def _graphql_transport(pages: dict[str, dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        name = body["query"].split("query ", 1)[1].split("(", 1)[0]
        return httpx.Response(200, json={"data": pages[name]})

    return httpx.MockTransport(handler)


RATE = {"cost": 1, "remaining": 4999, "resetAt": "2026-01-01T01:00:00Z"}


def _fake_pr():
    thread = {
        "id": "T1", "path": "src/a.py", "line": 3, "originalLine": 3, "startLine": None,
        "diffSide": "RIGHT", "isResolved": False, "isOutdated": True,
        "comments": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [{"id": "C1", "author": None, "body": "why?", "createdAt": "2026-01-01T00:00:00Z",
                       "url": "https://x", "diffHunk": "@@ -1 +1 @@", "originalCommit": {"oid": "c" * 40}}],
        },
    }
    pr = {
        "id": "PR_1", "title": "T", "url": "https://x", "author": {"login": "me"}, "baseRefName": "main",
        "headRefName": "f", "baseRefOid": "a" * 40, "headRefOid": "b" * 40, "changedFiles": 3,
        "latestReviews": {"nodes": [{"state": "CHANGES_REQUESTED", "author": {"login": "rev", "id": "U1"}}]},
        "reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [thread]},
    }
    convo = {
        "reviews": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [{"id": "R1", "author": {"login": "r"}, "state": "COMMENTED", "body": "",
                               "submittedAt": None, "url": "https://x"}]},
        "comments": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []},
    }
    gh = GitHubClient(
        token="t",
        transport=_graphql_transport({
            "PullRequest": {"repository": {"pullRequest": pr}, "rateLimit": RATE},
            "Conversation": {"repository": {"pullRequest": convo}, "rateLimit": RATE},
        }),
    )
    return fetch_pull_request(gh, PRRef(owner="o", repo="r", number=1)), gh


def test_fetch_builds_model():
    pr, gh = _fake_pr()
    t = pr.threads[0]
    assert t.comments[0].author == "ghost"
    assert t.original_commit == "c" * 40
    assert pr.reviewers[0].user_id == "U1"
    assert pr.reviews == []  # empty-body reviews are dropped
    assert gh.last_rate_limit.remaining == 4999


def test_submission_only_rerequests_blocking_reviewers():
    pr, _ = _fake_pr()
    now = datetime.now(UTC)
    state = ReviewState(
        decisions={"T1": ThreadDecision(decision="fix", reply="Done in abc1234.", resolve=True, decided_at=now)},
        plan=[WorkItem(id=1, title="Fix it", thread_ids=["T1"], status="committed", commit_sha="abc1234def")],
    )
    sub = submit.build_submission(pr, state, None)
    assert sub.rerequest == ["rev"]
    assert sub.replies[0].resolve is True
    assert "abc1234 Fix it (1 thread)" in sub.summary


def _repo(tmp_path: Path):
    def run(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    return run


def test_changed_files_handles_renames(tmp_path: Path):
    run = _repo(tmp_path)
    (tmp_path / "old.txt").write_text("line\n" * 20)
    (tmp_path / "keep.txt").write_text("a\n")
    run("add", ".")
    run("commit", "-qm", "base")
    run("mv", "old.txt", "new.txt")
    (tmp_path / "keep.txt").write_text("b\n")
    run("commit", "-qam", "head")

    files = {f.path: f for f in git.changed_files(tmp_path, "HEAD~1", "HEAD")}
    assert files["new.txt"].status == "R" and files["new.txt"].old_path == "old.txt"
    assert (files["keep.txt"].additions, files["keep.txt"].deletions) == (1, 1)


def test_trace_stale_moved_and_edited(tmp_path: Path):
    run = _repo(tmp_path)
    lines = [f"line {i}" for i in range(1, 21)]
    (tmp_path / "a.py").write_text("\n".join(lines) + "\n")
    run("add", ".")
    run("commit", "-qm", "base")
    base = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    # Insert 3 lines at the top (moves line 10 to 13) and rewrite old line 15.
    new = ["new a", "new b", "new c", *lines]
    new[14 + 3] = "line 15 rewritten"
    (tmp_path / "a.py").write_text("\n".join(new) + "\n")
    run("commit", "-qam", "edit")

    moved = git.trace_stale(tmp_path, "a.py", base, 10, "HEAD")
    assert (moved.status, moved.current_line) == ("moved", 13)
    assert moved.commits[0].subject == "edit"

    edited = git.trace_stale(tmp_path, "a.py", base, 15, "HEAD")
    assert edited.status == "edited"
    assert "line 15 rewritten" in edited.current_snippet

    run("rm", "-q", "a.py")
    run("commit", "-qm", "delete")
    assert git.trace_stale(tmp_path, "a.py", base, 10, "HEAD").status == "deleted"


def test_changed_paths_and_summary():
    from reviewr.checks import _summary, changed_paths

    patch = (
        "diff --git a/src/a.py b/src/a.py\nindex 1..2\n--- a/src/a.py\n+++ b/src/a.py\n"
        "diff --git a/src/old.py b/src/old.py\ndeleted file mode 100644\n"
    )
    paths = changed_paths(patch)
    assert [(p.path, p.deleted) for p in paths] == [("src/a.py", False), ("src/old.py", True)]
    assert _summary("x\nTests:       2 failed, 310 passed, 312 total\nRan all") == "Tests:       2 failed, 310 passed, 312 total"


def test_scoped_checks_only_touch_matching_files(tmp_path: Path):
    from reviewr.checks import run_checks
    from reviewr.config import CheckConfig, Config, RepoConfig

    run = _repo(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (tmp_path / "pkg" / "other.py").write_text("X = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text("from pkg.calc import add\n")
    (tmp_path / "tests" / "test_other.py").write_text("from pkg.other import X\n")
    (tmp_path / "e2e").mkdir()
    (tmp_path / "e2e" / "test_flow.py").write_text("import pkg.calc\n")
    run("add", ".")
    run("commit", "-qm", "base")

    repo = RepoConfig(
        path=tmp_path,
        exclude_tests=["e2e/*"],
        checks=[
            CheckConfig(name="echo files", kind="lint", match=["pkg/*.py"], run=["echo", "{files}"]),
            CheckConfig(name="echo tests", kind="test", match=["pkg/*.py"], discover="python", run=["echo", "{tests}"]),
            CheckConfig(name="js", kind="lint", match=["*.ts"], run=["false"]),
        ],
    )
    patch = "diff --git a/pkg/calc.py b/pkg/calc.py\n"
    cfg = Config(repos={"o/r": repo}, data_dir=tmp_path / "data")
    lint, tests, js = run_checks(cfg, "o/r", tmp_path, patch).runs
    assert lint.files == ["pkg/calc.py"] and lint.status == "passed"
    assert tests.tests == ["tests/test_calc.py"]  # not test_other.py, not the e2e test
    assert js.status == "skipped"


def test_jira_issue_payload(monkeypatch):
    from reviewr import jira
    from reviewr.config import JiraConfig

    monkeypatch.setenv("JIRA_EMAIL", "me@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["body"] = json.loads(request.content)
        return httpx.Response(201, json={"key": "MOS-1"})

    pr, _ = _fake_pr()
    cfg = JiraConfig(base_url="https://x.atlassian.net/", project="MOS")
    key, url = jira.create_issue(cfg, pr, pr.threads, {"T1": "Fix it"}, {}, transport=httpx.MockTransport(handler))
    assert (key, url) == ("MOS-1", "https://x.atlassian.net/browse/MOS-1")
    fields = sent["body"]["fields"]
    assert fields["project"] == {"key": "MOS"} and fields["summary"].startswith("Follow-up from PR #1")
    assert fields["description"]["content"][1]["type"] == "bulletList"


def test_env_installs_from_branch_lockfiles_and_links(tmp_path: Path):
    from reviewr import envs
    from reviewr.config import Config, EnvConfig, RepoConfig

    wt = tmp_path / "wt"
    (wt / "app").mkdir(parents=True)
    (wt / "app" / "deps.txt").write_text("a==1\n")
    env = EnvConfig(
        name="py", files=["app/deps.txt"],
        install=[["mkdir", "venv"], ["cp", "app/deps.txt", "venv/installed.txt"]],
        provides={"app/.venv": "venv"},
    )
    cfg = Config(repos={"o/r": RepoConfig(path=tmp_path, envs=[env])}, data_dir=tmp_path / "data")

    links = envs.link_envs(cfg, "o/r", wt)
    assert (wt / "app" / ".venv" / "installed.txt").read_text() == "a==1\n"
    first = envs.status(cfg, "o/r", envs.worktree_reader(wt))[0]
    assert first.status == "ready"
    envs.unlink(links)
    assert not (wt / "app" / ".venv").exists()

    # A branch with different lockfiles gets its own install.
    (wt / "app" / "deps.txt").write_text("a==2\n")
    second = envs.status(cfg, "o/r", envs.worktree_reader(wt))[0]
    assert second.key != first.key and second.status == "missing"


def test_seeded_install_clones_previous_env(tmp_path: Path):
    from reviewr import envs
    from reviewr.config import Config, EnvConfig, RepoConfig

    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "lock").write_text("v1")
    env = EnvConfig(
        name="js", files=["lock"],
        install=[["sh", "-c", "mkdir -p mods && echo full > mods/how && cp lock mods/lock"]],
        seed=["mods"],
        install_seeded=[["sh", "-c", "echo incremental > mods/how && cp lock mods/lock"]],
        provides={"node_modules": "mods"},
    )
    cfg = Config(repos={"o/r": RepoConfig(path=tmp_path, envs=[env])}, data_dir=tmp_path / "data")
    first = envs.ensure(cfg, "o/r", env, envs.worktree_reader(wt))
    assert (first / "mods" / "how").read_text().strip() == "full"

    (wt / "lock").write_text("v2")
    second = envs.ensure(cfg, "o/r", env, envs.worktree_reader(wt))
    assert second != first
    assert (second / "mods" / "how").read_text().strip() == "incremental"
    assert (second / "mods" / "lock").read_text() == "v2"
    assert (first / "mods" / "lock").read_text() == "v1"  # the original is untouched

    assert {i.key for i in envs.inventory(cfg)} == {first.name.rsplit("-", 1)[1], second.name.rsplit("-", 1)[1]}
    assert envs.remove([str(first)], cfg) == [str(first.resolve())]
    assert not first.exists() and second.exists()
    assert envs.remove([str(tmp_path)], cfg) == []  # never outside the env folder


def test_editor_argv_opens_worktree_as_project():
    from reviewr.config import DEFAULT_EDITORS
    from reviewr.editor import editor_argv

    root, f = Path("/wt"), Path("/wt/src/a.py")
    assert editor_argv(DEFAULT_EDITORS["vscode"], root, f, 12) == ["code", "/wt", "--goto", "/wt/src/a.py:12"]
    assert editor_argv(DEFAULT_EDITORS["pycharm"], root, f, 3) == ["pycharm", "/wt", "--line", "3", "/wt/src/a.py"]
    # Folder only: the file arguments and their flags are dropped.
    assert editor_argv(DEFAULT_EDITORS["vscode"], root, None, None) == ["code", "/wt"]
    assert editor_argv(DEFAULT_EDITORS["pycharm"], root, None, None) == ["pycharm", "/wt"]
    # Old-style templates without {root} still get the folder when no file is given.
    assert editor_argv(["zed", "{path}:{line}"], root, None, None) == ["zed", "/wt"]


def test_your_edits_are_seen_without_touching_your_index(tmp_path: Path):
    run = _repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 1\n")
    run("add", ".")
    run("commit", "-qm", "base")

    # Your edits: one tracked change, one new file. Nothing staged.
    (tmp_path / "b.py").write_text("y = 2\n")
    (tmp_path / "new.py").write_text("z = 1\n")
    diff = git.working_diff(tmp_path)
    assert "b/b.py" in diff and "b/new.py" in diff
    staged = subprocess.run(["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"], capture_output=True, text=True, check=True)
    assert staged.stdout == ""  # index untouched

    # An agent patch to a.py commits only a.py; your b.py and new.py stay uncommitted.
    patch = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    git.apply_and_commit(tmp_path, patch, "agent: a")
    files = subprocess.run(["git", "-C", str(tmp_path), "show", "--name-only", "--format=", "HEAD"], capture_output=True, text=True, check=True)
    assert files.stdout.split() == ["a.py"]
    assert (tmp_path / "b.py").read_text() == "y = 2\n"

    # A patch to a file you've edited is refused rather than mixed with your edits.
    patch_b = "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-y = 1\n+y = 3\n"
    with pytest.raises(git.GitError, match="uncommitted changes"):
        git.apply_and_commit(tmp_path, patch_b, "agent: b")


def test_branch_worktree_uses_your_local_branch_as_is(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run = _repo(repo)
    (repo / "f").write_text("1\n")
    run("add", ".")
    run("commit", "-qm", "base")
    run("branch", "feature")
    run("checkout", "-q", "feature")
    (repo / "f").write_text("2\n")
    run("commit", "-qam", "local work")
    tip = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    run("checkout", "-q", "main")

    wt = git.ensure_branch_worktree(repo, tmp_path / "wt", "feature", "main")
    assert git.current_branch(wt) == "feature"
    assert git.head_sha(wt) == tip  # not reset to the start point
    assert git.worktree_for_branch(repo, "feature").resolve() == wt.resolve()


def test_detached_worktree_at_pr_head_is_found(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run = _repo(repo)
    (repo / "f").write_text("1\n")
    run("add", ".")
    run("commit", "-qm", "base")
    head = git.head_sha(repo)
    run("worktree", "add", "--detach", str(tmp_path / "wt"), head)
    assert git.worktree_for_branch(repo, "feature") is None
    assert git.worktree_detached_at(repo, head).resolve() == (tmp_path / "wt").resolve()


def test_commit_paths_commits_only_chosen_files(tmp_path: Path):
    run = _repo(tmp_path)
    (tmp_path / "a.py").write_text("1\n")
    run("add", ".")
    run("commit", "-qm", "base")
    (tmp_path / "a.py").write_text("2\n")
    (tmp_path / "notes.json").write_text("{}")
    git.commit_paths(tmp_path, "fix a", ["a.py"])
    files = subprocess.run(["git", "-C", str(tmp_path), "show", "--name-only", "--format=", "HEAD"], capture_output=True, text=True, check=True)
    assert files.stdout.split() == ["a.py"]
    assert "notes.json" in git.working_diff(tmp_path)  # still uncommitted


def test_set_draft_sends_the_right_mutation():
    from reviewr.github import client as client_mod
    from reviewr.github.fetch import set_draft

    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        name = "convertPullRequestToDraft" if "ConvertToDraft" in body["query"] else "markPullRequestReadyForReview"
        return httpx.Response(200, json={"data": {name: {"pullRequest": {"isDraft": name == "convertPullRequestToDraft"}}}})

    old = client_mod.MUTATION_INTERVAL_S
    client_mod.MUTATION_INTERVAL_S = 0
    try:
        gh = GitHubClient(token="t", transport=httpx.MockTransport(handler))
        assert set_draft(gh, "PR_1", True) is True
        assert set_draft(gh, "PR_1", False) is False
    finally:
        client_mod.MUTATION_INTERVAL_S = old
    assert [("ConvertToDraft" in b["query"], b["variables"]["prId"]) for b in sent] == [(True, "PR_1"), (False, "PR_1")]


def test_close_pull_request_mutation():
    from reviewr.github import client as client_mod
    from reviewr.github.fetch import close_pull_request

    def handler(request: httpx.Request) -> httpx.Response:
        assert "closePullRequest" in json.loads(request.content)["query"]
        return httpx.Response(200, json={"data": {"closePullRequest": {"pullRequest": {"state": "CLOSED"}}}})

    old = client_mod.MUTATION_INTERVAL_S
    client_mod.MUTATION_INTERVAL_S = 0
    try:
        assert close_pull_request(GitHubClient(token="t", transport=httpx.MockTransport(handler)), "PR_1") == "CLOSED"
    finally:
        client_mod.MUTATION_INTERVAL_S = old


def test_no_action_posts_nothing_and_needs_no_reply():
    pr, _ = _fake_pr()
    now = datetime.now(UTC)
    state = ReviewState(decisions={"T1": ThreadDecision(decision="no_action", decided_at=now)})
    sub = submit.build_submission(pr, state, None)
    assert sub.replies == [] and sub.missing_replies == []
    assert "No action" not in sub.summary


def test_strict_schema_for_codex():
    from reviewr.llm.client import strict_schema
    from reviewr.llm.triage import _Result

    schema = strict_schema(_Result.model_json_schema())
    item = schema["$defs"]["_Item"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(item["properties"])  # optional fields included, as nullable
    assert {"type": "null"} in item["properties"]["stale_verdict"]["anyOf"]
    assert "title" in item["properties"] and "title" in item["required"]  # a field named title survives


def test_codex_structured_call(monkeypatch):
    from pydantic import BaseModel

    from reviewr import ai
    from reviewr.config import LLMConfig
    from reviewr.llm import client

    class Answer(BaseModel):
        word: str

    def fake_run(argv, **kw):
        out = Path(argv[argv.index("-o") + 1])
        assert "--output-schema" in argv and "read-only" in argv
        out.write_text('{"word": "fig"}')
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(ai, "current", lambda: "codex")
    monkeypatch.setattr(client.subprocess, "run", fake_run)
    assert client.structured(LLMConfig(), "sys", "user", Answer).word == "fig"
