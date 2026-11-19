"""SQLite cache (D8). UI navigation reads from here; only an explicit refresh calls GitHub.

Tables hold JSON blobs keyed by PR, which keeps the schema stable while the
models change and makes the cache easy to inspect with `sqlite3`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from reviewr.models import PRRef, PullRequest
from reviewr.state import ReviewState

SCHEMA = """
create table if not exists pull_requests (
    pr_key text primary key,
    json text not null,
    fetched_at text not null
);
create table if not exists review_state (
    pr_key text primary key,
    json text not null,
    updated_at text not null default (datetime('now'))
);
create table if not exists llm_cache (
    cache_key text primary key,
    json text not null,
    created_at text not null default (datetime('now'))
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        # One lock for all writes; background agent jobs update state concurrently
        # with API requests.
        self._lock = threading.RLock()

    # Pull requests

    def get_pr(self, ref: PRRef) -> PullRequest | None:
        row = self._db.execute(
            "select json from pull_requests where pr_key = ?", (ref.key,)
        ).fetchone()
        if not row:
            return None
        try:
            return PullRequest.model_validate_json(row[0])
        except ValidationError:
            return None  # cached before the model changed; refetch from GitHub

    def list_prs(self) -> list[PullRequest]:
        out = []
        for (js,) in self._db.execute("select json from pull_requests"):
            try:
                out.append(PullRequest.model_validate_json(js))
            except ValidationError:
                continue
        return out

    def put_pr(self, pr: PullRequest) -> None:
        with self._lock, self._db:
            self._db.execute(
                "insert or replace into pull_requests values (?, ?, ?)",
                (pr.ref.key, pr.model_dump_json(), pr.fetched_at.isoformat()),
            )

    # Review state

    def get_state(self, ref: PRRef) -> ReviewState:
        row = self._db.execute(
            "select json from review_state where pr_key = ?", (ref.key,)
        ).fetchone()
        return ReviewState.model_validate_json(row[0]) if row else ReviewState()

    def update_state(self, ref: PRRef, change: Callable[[ReviewState], None]) -> ReviewState:
        """Read, apply `change`, and write back under the lock."""
        with self._lock:
            state = self.get_state(ref)
            change(state)
            with self._db:
                self._db.execute(
                    "insert or replace into review_state (pr_key, json, updated_at) "
                    "values (?, ?, datetime('now'))",
                    (ref.key, state.model_dump_json()),
                )
            return state

    # LLM results

    def get_llm(self, key: str) -> Any | None:
        row = self._db.execute("select json from llm_cache where cache_key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_llm(self, key: str, value: Any) -> None:
        with self._lock, self._db:
            self._db.execute(
                "insert or replace into llm_cache (cache_key, json) values (?, ?)",
                (key, json.dumps(value)),
            )
