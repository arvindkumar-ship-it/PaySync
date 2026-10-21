"""Idempotency store: first side-effectful operation per key wins. Concurrent
duplicates BLOCK until the first call finishes and receive its real result
(or take over if it failed). Completed keys can be pre-seeded from the WAL so
idempotency survives restarts."""
from __future__ import annotations
import threading
from typing import Any, Callable, Dict, Optional, Tuple


class _Flight:
    __slots__ = ("done", "result")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: Any = None


class IdempotencyStore:
    def __init__(self, wait_timeout_s: float = 30.0) -> None:
        self._lock = threading.Lock()
        self._results: Dict[str, Any] = {}
        self._inflight: Dict[str, _Flight] = {}
        self.wait_timeout_s = wait_timeout_s

    def record(self, key: str, result: Any = None) -> None:
        """Mark a key as already completed (used when rebuilding from the WAL)."""
        with self._lock:
            self._results.setdefault(key, result)

    def execute(self, key: str, operation: Callable[[], Any]) -> Tuple[Any, bool]:
        while True:
            with self._lock:
                if key in self._results:
                    return self._results[key], False
                flight = self._inflight.get(key)
                if flight is None:
                    flight = self._inflight[key] = _Flight()
                    owner = True
                else:
                    owner = False
            if not owner:
                if not flight.done.wait(self.wait_timeout_s):
                    raise TimeoutError(f"operation {key!r} still in flight")
                continue  # re-check: result cached, or first call failed -> take over
            try:
                result = operation()
            except BaseException:
                with self._lock:
                    self._inflight.pop(key, None)
                flight.done.set()
                raise
            with self._lock:
                self._results[key] = result
                self._inflight.pop(key, None)
            flight.done.set()
            return result, True

    def peek(self, key: str) -> Optional[Any]:
        with self._lock:
            return self._results.get(key)
