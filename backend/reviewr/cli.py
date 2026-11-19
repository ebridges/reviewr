"""Command line entry point.

    reviewr serve                 start the API on 127.0.0.1:8765
    reviewr fetch <PR URL>        fetch a PR into the cache and print a summary
"""

from __future__ import annotations

import argparse
import logging
from collections import Counter


def main() -> None:
    parser = argparse.ArgumentParser(prog="reviewr")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--reload", action="store_true")
    fetch = sub.add_parser("fetch")
    fetch.add_argument("pr", help="PR URL or owner/repo#N")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("reviewr.api:app", host="127.0.0.1", port=args.port, reload=args.reload)
    elif args.cmd == "fetch":
        _fetch(args.pr)


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
