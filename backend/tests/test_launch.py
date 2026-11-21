import json
import subprocess
from pathlib import Path

import httpx

from reviewr import launch
from reviewr.github.client import GitHubClient
from reviewr.models import PRRef


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
    return repo


def test_github_slug_from_remote_urls():
    assert launch.github_slug("git@github.com:acme/app.git") == "acme/app"
    assert launch.github_slug("https://github.com/acme/app") == "acme/app"
    assert launch.github_slug("https://github.com/acme/app.git/") == "acme/app"
    assert launch.github_slug("ssh://git@github.com/acme/my.app.git") == "acme/my.app"
    assert launch.github_slug("git@gitlab.com:acme/app.git") is None


def test_read_checkout_follows_tracking_branch_and_prefers_upstream(tmp_path: Path):
    repo = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", "git@github.com:me/app.git")
    _git(repo, "remote", "add", "upstream", "https://github.com/acme/app.git")
    _git(repo, "checkout", "-q", "-b", "local-name")
    _git(repo, "config", "branch.local-name.remote", "origin")
    _git(repo, "config", "branch.local-name.merge", "refs/heads/feature/x")

    co = launch.read_checkout(repo / ".")
    assert co.root == repo.resolve() and co.clone == repo.resolve()
    assert (co.branch, co.remote_branch, co.push_slug) == ("local-name", "feature/x", "me/app")
    assert co.slugs == ["acme/app", "me/app"]


def test_read_checkout_in_a_linked_worktree(tmp_path: Path):
    repo = _repo(tmp_path)
    _git(repo, "remote", "add", "origin", "git@github.com:acme/app.git")
    _git(repo, "worktree", "add", "-q", "-b", "fix-it", str(tmp_path / "wt"))

    co = launch.read_checkout(tmp_path / "wt")
    assert co.root == (tmp_path / "wt").resolve() and co.clone == repo.resolve()
    assert (co.branch, co.remote_branch, co.slugs) == ("fix-it", "fix-it", ["acme/app"])


def test_read_checkout_outside_git(tmp_path: Path):
    try:
        launch.read_checkout(tmp_path)
    except LookupError as e:
        assert "isn't in a git checkout" in str(e)
    else:
        raise AssertionError("expected LookupError")


def _client(answers: dict[str, list[dict]]) -> tuple[GitHubClient, list[dict]]:
    """answers maps owner/repo to the PR nodes GitHub returns for it."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        v = json.loads(request.content)["variables"]
        seen.append(v)
        nodes = answers.get(f"{v['owner']}/{v['repo']}", [])
        return httpx.Response(200, json={"data": {"repository": {"pullRequests": {"nodes": nodes}}}})

    return GitHubClient(token="t", transport=httpx.MockTransport(handler)), seen


def _node(number: int, head: str) -> dict:
    owner, name = head.split("/")
    return {"number": number, "title": f"PR {number}", "headRepository": {"owner": {"login": owner}, "name": name}}


def test_find_pr_skips_forks_with_the_same_branch_name():
    co = launch.Checkout(Path("."), Path("."), "x", "feature/x", "me/app", ["acme/app", "me/app"])
    gh, seen = _client({"acme/app": [_node(9, "someone/app"), _node(7, "me/app")]})
    assert launch.find_pr(gh, co) == (PRRef(owner="acme", repo="app", number=7), "PR 7")
    assert seen == [{"owner": "acme", "repo": "app", "branch": "feature/x"}]


def test_find_pr_tries_the_next_repo_and_gives_up():
    co = launch.Checkout(Path("."), Path("."), "x", "x", "me/app", ["acme/app", "me/app"])
    gh, seen = _client({"me/app": [_node(3, "me/app")]})
    assert launch.find_pr(gh, co) == (PRRef(owner="me", repo="app", number=3), "PR 3")
    gh, seen = _client({})
    assert launch.find_pr(gh, co) is None and len(seen) == 2


def test_pr_route_matches_frontend_hrefs():
    assert launch.pr_route(PRRef(owner="a", repo="b.c", number=4)) == "#/pr/a/b.c/4/overview"


def test_app_bundle_found_from_the_packaged_backend(tmp_path: Path):
    exe = tmp_path / "reviewr.app" / "Contents" / "Resources" / "backend" / "reviewr" / "reviewr"
    exe.parent.mkdir(parents=True)
    exe.touch()
    assert launch._app_bundle(exe) == (tmp_path / "reviewr.app").resolve()
    assert launch._app_bundle(tmp_path / "x") is None
