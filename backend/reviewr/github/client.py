"""GitHub GraphQL client.

See docs/github-api-strategy.md for why reviewr uses GraphQL only (D2-D5, D10).
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.github.com/graphql"
REST_URL = "https://api.github.com"
QUERY_DIR = Path(__file__).parent / "queries"

# D6: minimum gap between mutations, to stay under GitHub's secondary
# content-creation limit (80/minute).
MUTATION_INTERVAL_S = 1.0


class GitHubError(RuntimeError):
    pass


@dataclass
class RateLimit:
    cost: int
    remaining: int
    reset_at: str


def get_token() -> str:
    """D9: reuse the gh CLI login; GITHUB_TOKEN overrides."""
    if token := os.environ.get("GITHUB_TOKEN"):
        return token
    try:
        out = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True, timeout=10
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise GitHubError("No GitHub token. Run `gh auth login` or set GITHUB_TOKEN.") from e
    return out.stdout.strip()


def load_query(name: str) -> str:
    return (QUERY_DIR / f"{name}.graphql").read_text()


class GitHubClient:
    def __init__(self, token: str | None = None, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            headers={"Authorization": f"Bearer {token or get_token()}"},
            timeout=30,
            transport=transport,
        )
        self.last_rate_limit: RateLimit | None = None
        self._last_mutation = 0.0

    def query(self, name: str, **variables: Any) -> dict[str, Any]:
        """Run the query in queries/<name>.graphql and return `data`."""
        return self._post(load_query(name), variables)

    def mutate(self, name: str, **variables: Any) -> dict[str, Any]:
        """Run one mutation (D6: one per request, throttled)."""
        wait = self._last_mutation + MUTATION_INTERVAL_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            return self._post(load_query(name), variables)
        finally:
            self._last_mutation = time.monotonic()

    def rest(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """D11: REST, only for what GraphQL doesn't offer: Actions job logs and re-runs."""
        resp = self._http.request(method, REST_URL + path, follow_redirects=True, **kwargs)
        if resp.status_code >= 400:
            raise GitHubError(f"GitHub returned HTTP {resp.status_code} for {path}: {resp.text[:300]}")
        return resp

    def _post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        resp = self._http.post(GRAPHQL_URL, json={"query": query, "variables": variables})
        if resp.status_code != 200:
            raise GitHubError(f"GitHub returned HTTP {resp.status_code}: {resp.text[:500]}")
        payload = resp.json()
        if errors := payload.get("errors"):
            raise GitHubError("; ".join(e.get("message", str(e)) for e in errors))
        data = payload["data"]
        # D7: every query asks for rateLimit so cost is measured, not guessed.
        if rl := data.get("rateLimit"):
            self.last_rate_limit = RateLimit(rl["cost"], rl["remaining"], rl["resetAt"])
            log.info("GitHub GraphQL cost=%s remaining=%s", rl["cost"], rl["remaining"])
        return data
