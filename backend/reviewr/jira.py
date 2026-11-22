"""Jira Cloud: create one follow-up issue for deferred review threads.

Auth is Jira Cloud's basic auth: an Atlassian account email (in Settings) and an API
token (https://id.atlassian.com/manage-profile/security/api-tokens) saved in the
Keychain from Settings. $JIRA_EMAIL and $JIRA_API_TOKEN still work as overrides.
Uses the REST API v3.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from reviewr import secrets
from reviewr.config import JiraConfig
from reviewr.models import PullRequest, Thread


class JiraError(RuntimeError):
    pass


def _auth(cfg: JiraConfig) -> tuple[str, str]:
    email = os.environ.get("JIRA_EMAIL") or cfg.email
    token = os.environ.get("JIRA_API_TOKEN") or secrets.get_secret(secrets.JIRA_API_TOKEN)
    if not email or not token:
        raise JiraError("Add your Atlassian email and a Jira API token under Settings → Jira.")
    return email, token


def _client(cfg: JiraConfig, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        base_url=cfg.base_url.rstrip("/"), auth=_auth(cfg), timeout=30,
        headers={"Accept": "application/json"}, transport=transport,
    )


def whoami(cfg: JiraConfig, transport: httpx.BaseTransport | None = None) -> str:
    with _client(cfg, transport) as c:
        r = c.get("/rest/api/3/myself")
    if r.status_code != 200:
        raise JiraError(f"Jira returned {r.status_code}: {r.text[:200]}")
    return r.json().get("displayName") or r.json().get("emailAddress") or "connected"


def _text(s: str) -> dict[str, Any]:
    return {"type": "text", "text": s}


def _link(s: str, href: str) -> dict[str, Any]:
    return {"type": "text", "text": s, "marks": [{"type": "link", "attrs": {"href": href}}]}


def issue_body(pr: PullRequest, threads: list[Thread], titles: dict[str, str], notes: dict[str, str]) -> dict[str, Any]:
    """Atlassian Document Format: a short intro and one bullet per thread."""
    items = []
    for t in threads:
        first = t.comments[0] if t.comments else None
        content = [_link(titles.get(t.id, t.path), t.url), _text(f" — {t.path}")]
        if first:
            excerpt = first.body.strip().replace("\n", " ")
            content.append(_text(f"\n{first.author}: {excerpt[:400]}"))
        if notes.get(t.id):
            content.append(_text(f"\nNote: {notes[t.id]}"))
        items.append({"type": "listItem", "content": [{"type": "paragraph", "content": content}]})
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [
                _text("Review comments deferred from "), _link(f"{pr.ref.key}: {pr.title}", pr.url), _text("."),
            ]},
            {"type": "bulletList", "content": items},
        ],
    }


def create_issue(
    cfg: JiraConfig, pr: PullRequest, threads: list[Thread], titles: dict[str, str], notes: dict[str, str],
    transport: httpx.BaseTransport | None = None,
) -> tuple[str, str]:
    payload = {
        "fields": {
            "project": {"key": cfg.project},
            "issuetype": {"name": cfg.issue_type},
            "summary": f"Follow-up from PR #{pr.ref.number}: {pr.title}"[:250],
            "description": issue_body(pr, threads, titles, notes),
        }
    }
    with _client(cfg, transport) as c:
        r = c.post("/rest/api/3/issue", json=payload)
    if r.status_code not in (200, 201):
        raise JiraError(f"Jira returned {r.status_code}: {r.text[:300]}")
    key = r.json()["key"]
    return key, f"{cfg.base_url.rstrip('/')}/browse/{key}"
