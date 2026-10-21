"""Thread-safe counters, exposed on /status and drawn on the dashboard."""

from __future__ import annotations

import threading

COUNTERS = (
    "cache_hits",  # served from a fresh cache entry
    "cache_misses",  # had to ask PostgreSQL
    "cache_errors",  # Redis call failed or timed out
    "cache_bypassed",  # Redis circuit open, cache skipped without waiting
    "db_calls",  # logical database operations attempted
    "db_errors",  # database operations that failed after all retries
    "db_retries",  # extra attempts made after a dropped/refused connection
    "db_rejected",  # database circuit open, request failed fast
    "stale_served",  # PostgreSQL down, an older cached copy was returned
)


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values = dict.fromkeys(COUNTERS, 0)

    def incr(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._values[name] = self._values.get(name, 0) + amount

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._values)
