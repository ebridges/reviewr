"""Read pull requests from GitHub into models (D2, D3)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from reviewr.github.client import GitHubClient, GitHubError
from reviewr.models import Comment, PRRef, PRSummary, PullRequest, Review, ReviewerStatus, Thread

PR_LISTS = {
    "mine": "is:pr is:open author:@me archived:false sort:updated-desc",
    "review": "is:pr is:open review-requested:@me archived:false sort:updated-desc",
}


def _login(node: dict[str, Any]) -> str:
    # author is null for deleted accounts
    return (node.get("author") or {}).get("login", "ghost")


def _comment(node: dict[str, Any]) -> Comment:
    return Comment(
        id=node["id"],
        database_id=node.get("databaseId"),
        author=_login(node),
        body=node["body"],
        created_at=node["createdAt"],
        url=node["url"],
    )


def _pr_node(data: dict[str, Any], ref: PRRef) -> dict[str, Any]:
    pr = (data.get("repository") or {}).get("pullRequest")
    if pr is None:
        raise GitHubError(f"{ref.key} not found, or the token can't read it")
    return pr


def list_pull_requests(gh: GitHubClient, which: str) -> list[PRSummary]:
    data = gh.query("search_prs", q=PR_LISTS[which])
    out = []
    for n in data["search"]["nodes"]:
        if not n:  # search can return non-PR nodes as empty objects
            continue
        commits = n["commits"]["nodes"]
        rollup = (commits[0]["commit"].get("statusCheckRollup") or {}) if commits else {}
        out.append(
            PRSummary(
                ref=PRRef(
                    owner=n["repository"]["owner"]["login"],
                    repo=n["repository"]["name"],
                    number=n["number"],
                ),
                node_id=n["id"],
                title=n["title"],
                url=n["url"],
                is_draft=n["isDraft"],
                updated_at=n["updatedAt"],
                head_ref=n["headRefName"],
                changed_files=n["changedFiles"],
                review_decision=n["reviewDecision"],
                open_threads=sum(not t["isResolved"] for t in n["reviewThreads"]["nodes"]),
                ci=rollup.get("state"),
            )
        )
    return out


def fetch_pull_request(gh: GitHubClient, ref: PRRef) -> PullRequest:
    vars_ = {"owner": ref.owner, "repo": ref.repo, "number": ref.number}

    threads: list[Thread] = []
    meta: dict[str, Any] | None = None
    after = None
    while True:
        pr = _pr_node(gh.query("pull_request", **vars_, threadsAfter=after), ref)
        meta = meta or pr
        page = pr["reviewThreads"]
        threads.extend(_thread(gh, node) for node in page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]

    reviews: list[Review] = []
    issue_comments: list[Comment] = []
    cursors: dict[str, str | None] = {"reviewsAfter": None, "commentsAfter": None}
    more_reviews = more_comments = True
    while more_reviews or more_comments:
        pr = _pr_node(gh.query("conversation", **vars_, **cursors), ref)
        if more_reviews:
            r = pr["reviews"]
            reviews.extend(
                Review(
                    id=n["id"],
                    author=_login(n),
                    state=n["state"],
                    body=n["body"],
                    submitted_at=n["submittedAt"],
                    url=n["url"],
                )
                for n in r["nodes"]
                if n["body"].strip()
            )
            more_reviews = r["pageInfo"]["hasNextPage"]
            cursors["reviewsAfter"] = r["pageInfo"]["endCursor"]
        if more_comments:
            c = pr["comments"]
            issue_comments.extend(_comment(n) for n in c["nodes"])
            more_comments = c["pageInfo"]["hasNextPage"]
            cursors["commentsAfter"] = c["pageInfo"]["endCursor"]

    assert meta is not None
    return PullRequest(
        ref=ref,
        node_id=meta["id"],
        title=meta["title"],
        url=meta["url"],
        author=_login(meta),
        base_ref=meta["baseRefName"],
        head_ref=meta["headRefName"],
        base_oid=meta["baseRefOid"],
        head_oid=meta["headRefOid"],
        changed_files=meta["changedFiles"],
        threads=threads,
        reviews=reviews,
        reviewers=[
            ReviewerStatus(
                login=_login(n), user_id=(n.get("author") or {}).get("id"), state=n["state"]
            )
            for n in meta["latestReviews"]["nodes"]
        ],
        issue_comments=issue_comments,
        fetched_at=datetime.now(UTC),
    )


def _thread(gh: GitHubClient, node: dict[str, Any]) -> Thread:
    comment_nodes = list(node["comments"]["nodes"])
    if node["comments"]["pageInfo"]["hasNextPage"]:
        comment_nodes = _all_thread_comments(gh, node["id"])
    first = comment_nodes[0] if comment_nodes else {}
    return Thread(
        id=node["id"],
        path=node["path"],
        line=node["line"],
        original_line=node["originalLine"],
        start_line=node["startLine"],
        diff_side=node["diffSide"],
        is_resolved=node["isResolved"],
        is_outdated=node["isOutdated"],
        original_commit=(first.get("originalCommit") or {}).get("oid"),
        diff_hunk=first.get("diffHunk", ""),
        comments=[_comment(n) for n in comment_nodes],
    )


def _all_thread_comments(gh: GitHubClient, thread_id: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    after = None
    while True:
        page = gh.query("thread_comments", id=thread_id, after=after)["node"]["comments"]
        nodes.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            return nodes
        after = page["pageInfo"]["endCursor"]


def set_draft(gh: GitHubClient, node_id: str, draft: bool) -> bool:
    """Convert a PR to a draft, or mark it ready for review (which notifies reviewers).
    Returns the PR's draft status afterwards."""
    if draft:
        data = gh.mutate("convert_to_draft", prId=node_id)["convertPullRequestToDraft"]
    else:
        data = gh.mutate("mark_ready", prId=node_id)["markPullRequestReadyForReview"]
    return data["pullRequest"]["isDraft"]


def close_pull_request(gh: GitHubClient, node_id: str) -> str:
    """Close a PR without merging. Returns its state afterwards ("CLOSED")."""
    return gh.mutate("close_pr", prId=node_id)["closePullRequest"]["pullRequest"]["state"]
