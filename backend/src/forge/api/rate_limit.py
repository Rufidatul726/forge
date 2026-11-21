from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from threading import Lock
from time import monotonic


class SlidingWindowRateLimiter:
    """A bounded, process-local sliding-window request limiter."""

    def __init__(
        self,
        limit: int,
        window_seconds: int,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, client_id: str) -> tuple[bool, int]:
        """Record a request and return whether it is allowed and retry delay."""
        now = self._clock()
        cutoff = now - self.window_seconds
        with self._lock:
            requests = self._requests[client_id]
            while requests and requests[0] <= cutoff:
                requests.popleft()

            if len(requests) >= self.limit:
                retry_after = max(1, int(requests[0] + self.window_seconds - now) + 1)
                return False, retry_after

            requests.append(now)
            return True, 0
