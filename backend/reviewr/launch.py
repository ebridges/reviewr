"""`reviewr open`: find the PR for the branch checked out in a folder and open the app on it.

The folder's GitHub repo comes from its git remotes. The PR is the open one whose head
is the branch the checkout tracks, pushed to the same repo (so a fork's branch with the
same name doesn't match). The desktop app is started with the PR's route; if it's
already running, Electron's single-instance handler passes the route to the open window.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from reviewr.git import GitError, _git, current_branch
from reviewr.github.client import GitHubClient
from reviewr.models import PRRef

ROOT = Path(__file__).resolve().parents[2]

# git@github.com:o/r.git, https://github.com/o/r, ssh://git@github.com/o/r.git
_GITHUB_REMOTE = re.compile(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")

# At most this many repos are searched for the branch's PR (one query each).
MAX_REPOS = 3


class LaunchError(RuntimeError):
    pass


def github_slug(url: str) -> str | None:
    """owner/repo for a github.com remote URL, None for anything else."""
    m = _GITHUB_REMOTE.search(url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


@dataclass
class Checkout:
    root: Path  # top of the working tree
    clone: Path  # the main clone, when `root` is a linked worktree
    branch: str | None  # local branch, None when HEAD is detached
    remote_branch: str | None  # the branch's name on the remote it tracks
    push_slug: str | None  # GitHub repo the branch is pushed to
    slugs: list[str] = field(default_factory=list)  # repos to search for the PR, likeliest first


def read_checkout(path: Path) -> Checkout:
    try:
        root = Path(_git(path, "rev-parse", "--show-toplevel").stdout.strip())
        common = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    except GitError:
        raise LookupError(f"{path} isn't in a git checkout.") from None
    clone = common.parent if common.name == ".git" else common

    remotes: dict[str, str] = {}
    for line in _git(root, "remote", "-v", check=False).stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == "(fetch)" and (slug := github_slug(parts[1])):
            remotes[parts[0]] = slug

    branch = current_branch(root)
    tracked_remote = merge = None
    if branch:
        tracked_remote = _git(root, "config", f"branch.{branch}.remote", check=False).stdout.strip() or None
        merge = _git(root, "config", f"branch.{branch}.merge", check=False).stdout.strip() or None
    remote_branch = merge.removeprefix("refs/heads/") if merge else branch
    push_slug = remotes.get(tracked_remote or "origin") or remotes.get("origin")

    # In a fork, "upstream" is usually the repo the PR was opened against.
    order = ["upstream", tracked_remote, "origin", *remotes]
    slugs: list[str] = []
    for name in order:
        if name and (slug := remotes.get(name)) and slug not in slugs:
            slugs.append(slug)
    return Checkout(root, clone, branch, remote_branch, push_slug, slugs)


def find_pr(gh: GitHubClient, checkout: Checkout) -> tuple[PRRef, str] | None:
    """The open PR for the checkout's branch, with its title."""
    if not checkout.remote_branch:
        return None
    for slug in checkout.slugs[:MAX_REPOS]:
        owner, repo = slug.split("/", 1)
        data = gh.query("prs_for_branch", owner=owner, repo=repo, branch=checkout.remote_branch)
        nodes = ((data.get("repository") or {}).get("pullRequests") or {}).get("nodes") or []
        for node in nodes:
            head = node.get("headRepository")
            head_slug = f"{head['owner']['login']}/{head['name']}" if head else None
            if checkout.push_slug and head_slug and head_slug.lower() != checkout.push_slug.lower():
                continue
            return PRRef(owner=owner, repo=repo, number=node["number"]), node["title"]
    return None


def pr_route(ref: PRRef, page: str = "overview") -> str:
    """Same as prHref in frontend/src/lib.ts."""
    return f"#/pr/{ref.owner}/{ref.repo}/{ref.number}/{page}"


def _frontend_stale(frontend: Path) -> bool:
    built = frontend / "dist" / "index.html"
    if not built.is_file():
        return True
    newest = max(
        (p.stat().st_mtime for p in [*(frontend / "src").rglob("*"), frontend / "index.html"] if p.is_file()),
        default=0.0,
    )
    return newest > built.stat().st_mtime


def _app_bundle(executable: Path) -> Path | None:
    """The .app holding this executable, when running from the packaged app."""
    return next((p for p in executable.resolve().parents if p.suffix == ".app"), None)


def _spawn(args: list[str], cwd: Path | None = None) -> None:
    subprocess.Popen(
        args,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def launch_app(route: str | None) -> None:
    """Start the desktop app (or switch the running one) on `route`. Returns at once."""
    route_args = [f"--reviewr-route={route}"] if route else []
    if getattr(sys, "frozen", False):
        # Packaged: this binary is reviewr.app/Contents/Resources/backend/reviewr/reviewr.
        # Running the app's own executable (not `open -a`) passes the route to an already
        # running copy through Electron's single-instance handler.
        bundle = _app_bundle(Path(sys.executable))
        if bundle is None:
            raise LaunchError(f"Can't find the reviewr app around {sys.executable}.")
        _spawn([str(bundle / "Contents" / "MacOS" / "reviewr"), *route_args])
        return

    desktop = ROOT / "desktop"
    electron = desktop / "node_modules" / ".bin" / "electron"
    if not electron.exists():
        raise LaunchError(f"The desktop app isn't installed. Run `npm run setup` in {ROOT}.")
    frontend = ROOT / "frontend"
    if os.environ.get("REVIEWR_DEV") != "1" and _frontend_stale(frontend):
        print("Building the frontend…", flush=True)
        build = subprocess.run(
            ["npm", "--prefix", str(frontend), "run", "build"], check=False, capture_output=True, text=True
        )
        if build.returncode != 0:
            raise LaunchError(f"The frontend build failed:\n{(build.stdout + build.stderr)[-2000:]}")
    _spawn([str(electron), str(desktop), *route_args], cwd=ROOT)
