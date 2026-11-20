"""Which PRs have background jobs running in this process, counted per PR, so one job
finishing doesn't make another look interrupted."""

from __future__ import annotations

import threading
from collections import Counter


class Jobs:
    def __init__(self) -> None:
        self._n: Counter[str] = Counter()
        self._lock = threading.Lock()

    def add(self, key: str) -> None:
        """Call once per job, before it's queued."""
        with self._lock:
            self._n[key] += 1

    def discard(self, key: str) -> None:
        """Call once per job, when it finishes."""
        with self._lock:
            self._n[key] -= 1
            if self._n[key] <= 0:
                del self._n[key]

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return self._n.get(key, 0) > 0
