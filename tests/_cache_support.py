"""A seam for counting the cache's blocked followers, so concurrency tests need no sleeps."""

import threading
from types import SimpleNamespace

import pytest

from herdr_pwa.clock import MonoTime
from tests.engine_seam import cache_module


class FollowerGate:
    """Counts threads blocked on a cache fetch, and can hold them once they wake."""

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.blocked = 0
        self.woken = threading.Event()
        self.woken.set()
        gate = self

        class _CountedEvent(threading.Event):
            def wait(self, timeout: float | None = None) -> bool:
                with gate.cond:
                    gate.blocked += 1
                    gate.cond.notify_all()
                result = super().wait(timeout)
                assert gate.woken.wait(10), "the test never let the woken follower go"
                return result

        self.event_type = _CountedEvent

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Make every fetch the cache creates from now on report its waiters here."""
        monkeypatch.setattr(
            cache_module, "threading", SimpleNamespace(Event=self.event_type, Lock=threading.Lock)
        )

    def until(self, count: int, timeout: float = 10.0) -> bool:
        """Block until ``count`` followers are waiting on a fetch."""
        with self.cond:
            return self.cond.wait_for(lambda: self.blocked >= count, timeout)


class TestClock:
    """A monotonic clock a test advances by hand, shared safely between threads."""

    __test__ = False

    def __init__(self, now: float = 0.0) -> None:
        self._lock = threading.Lock()
        self.now = now

    def __call__(self) -> MonoTime:
        with self._lock:
            return MonoTime(self.now)

    def advance(self, seconds: float) -> None:
        """Move time forward."""
        with self._lock:
            self.now += seconds
