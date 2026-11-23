"""Command line entry point.

    reviewr                       same as `reviewr open`
    reviewr open [FOLDER | PR]    open the app on the PR for the folder's branch (default: .)
    reviewr serve                 start the API on 127.0.0.1:8765
    reviewr fetch <PR URL>        fetch a PR into the cache and print a summary
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path


def main() -> None:
    from reviewr import __version__

    parser = argparse.ArgumentParser(prog="reviewr")
    parser.add_argument("--version", action="version", version=f"reviewr {__version__}")
    sub = parser.add_subparsers(dest="cmd")
    open_ = sub.add_parser("open", help="open the app on the PR for a folder's branch")
    open_.add_argument("target", nargs="?", default=".", help="a folder (default: .), a PR URL or owner/repo#N")
    open_.add_argument("--no-launch", action="store_true", help="print the PR without opening the app")
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--reload", action="store_true")
    fetch = sub.add_parser("fetch")
    fetch.add_argument("pr", help="PR URL or owner/repo#N")
    args = parser.parse_args()

    if args.cmd in (None, "open"):
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
        sys.exit(_open(getattr(args, "target", "."), launch=not getattr(args, "no_launch", False)))

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.cmd == "serve":
        import uvicorn

        from reviewr.shellenv import adopt_login_env

        if changed := adopt_login_env():
            logging.getLogger("reviewr").info("Using your login shell's environment for: %s", ", ".join(changed))

        uvicorn.run("reviewr.api:app", host="127.0.0.1", port=args.port, reload=args.reload)
    elif args.cmd == "fetch":
        _fetch(args.pr)


def _open(target: str, launch: bool) -> int:
    from reviewr import launch as launcher
    from reviewr.config import RepoConfig, load_config, update_config
    from reviewr.github.client import GitHubClient, GitHubError
    from reviewr.models import PRRef

    checkout = None
    try:
        ref, title = PRRef.parse(target), None
    except ValueError:
        ref = title = None
        path = Path(target).expanduser()
        if not path.is_dir():
            print(f"reviewr: {target} is neither a folder nor a PR (URL or owner/repo#N).", file=sys.stderr)
            return 2
        try:
            checkout = launcher.read_checkout(path)
        except LookupError as e:
            print(f"{e} Opening the PR list.")
        else:
            if not checkout.branch:
                print("HEAD is detached, so there's no branch to look up. Opening the PR list.")
            elif not checkout.slugs:
                print(f"{checkout.root} has no github.com remote. Opening the PR list.")
            else:
                try:
                    found = launcher.find_pr(GitHubClient(), checkout)
                except GitHubError as e:
                    print(f"Couldn't look up the PR: {e}\nOpening the PR list.")
                    found = None
                else:
                    if not found:
                        repos = ", ".join(checkout.slugs[: launcher.MAX_REPOS])
                        print(f"No open PR for {checkout.remote_branch} in {repos}. Opening the PR list.")
                if found:
                    ref, title = found

    if ref:
        print(f"{ref.key}: {title}" if title else ref.key)
        if ref.slug not in load_config().repos:
            if checkout is not None:
                # You ran this in a clone of the repo: use it.
                update_config(lambda c: c.repos.__setitem__(ref.slug, RepoConfig(path=checkout.clone)))
                print(f"Using {checkout.clone} as reviewr's clone of {ref.slug}.")
            else:
                print(f"{ref.slug} isn't set up in reviewr yet; the app starts with workspace setup.")
    if launch:
        try:
            launcher.launch_app(launcher.pr_route(ref) if ref else None)
        except launcher.LaunchError as e:
            print(f"reviewr: {e}", file=sys.stderr)
            return 1
    return 0


def _fetch(text: str) -> None:
    from reviewr import api, git
    from reviewr.models import PRRef

    ref = PRRef.parse(text)
    pr = api._load_pr(ref, refresh=True)
    open_threads = [t for t in pr.threads if not t.is_resolved]
    print(f"{pr.ref.key}: {pr.title}")
    print(f"  {pr.head_ref} -> {pr.base_ref}  ({pr.head_oid[:10]})")
    print(
        f"  threads: {len(pr.threads)} ({len(open_threads)} open, "
        f"{sum(t.is_outdated for t in pr.threads)} outdated), "
        f"comments: {sum(len(t.comments) for t in pr.threads)}, "
        f"reviews with a body: {len(pr.reviews)}, conversation comments: {len(pr.issue_comments)}"
    )
    by_dir = Counter(t.path.rsplit("/", 1)[0] if "/" in t.path else "." for t in open_threads)
    for d, n in by_dir.most_common(5):
        print(f"    {n:3}  {d}")
    if rl := api.github().last_rate_limit:
        print(f"  GitHub GraphQL: last query cost {rl.cost}, {rl.remaining} points remaining")
    try:
        repo = api.load_config().repo_path(ref.slug)
        git.ensure_commits(repo, pr.base_oid, pr.head_oid)
        print(f"  local diff: {len(git.changed_files(repo, pr.base_oid, pr.head_oid))} files")
    except (LookupError, git.GitError) as e:
        print(f"  local diff unavailable: {e}")


if __name__ == "__main__":
    main()
