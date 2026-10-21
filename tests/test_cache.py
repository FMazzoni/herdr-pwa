"""Cache tests: coalescing and an honest age, proved rather than asserted."""

import threading
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from herdr_pwa.clock import MonoTime
from herdr_pwa.engine import (
    CachedSnapshot,
    EngineUnavailableError,
    Snapshot,
    SnapshotCache,
)
from tests._cache_support import FollowerGate
from tests._jsonpath import obj

FIXTURE = Path(__file__).parent / "fixtures" / "snapshot.example.json"
_RAW = obj(TypeAdapter(JsonValue).validate_json(FIXTURE.read_text()), "result", "snapshot")
SNAPSHOT = Snapshot.from_raw(_RAW)


class Clock:
    """A monotonic clock a test advances by hand, so ages need no sleeping."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> MonoTime:
        return MonoTime(self.now)

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Loader:
    """A counted stand-in for ``client.snapshot``."""

    def __init__(
        self,
        snapshot: Snapshot = SNAPSHOT,
        *,
        error: Exception | None = None,
        hold: threading.Event | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._hold = hold
        self.error = error
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, **_: object) -> Snapshot:
        with self._lock:
            self.calls += 1
        if self._hold is not None:
            # Held until every caller has arrived, so "concurrent" is true rather than hoped for.
            assert self._hold.wait(5), "the test never released the fetch"
        if self.error is not None:
            raise self.error
        return self._snapshot


# Coalescing — the property a sequential test cannot see

CALLERS = 10


def test_concurrent_misses_coalesce_onto_one_fetch(monkeypatch: pytest.MonkeyPatch):
    """Ten simultaneous misses on a cold cache must cost exactly one engine call."""
    gate = FollowerGate()
    gate.install(monkeypatch)
    cache = SnapshotCache(ttl=60.0)
    hold = threading.Event()
    load = Loader(hold=hold)
    ready = threading.Barrier(CALLERS)
    results: list[CachedSnapshot] = []
    failures: list[BaseException] = []

    def caller() -> None:
        try:
            ready.wait(5)  # nobody starts until everybody can
            results.append(cache.get(load))
        except BaseException as exc:  # noqa: BLE001 - carried to the test thread, which fails on it
            failures.append(exc)

    threads = [threading.Thread(target=caller) for _ in range(CALLERS)]
    for thread in threads:
        thread.start()
    # Every follower is blocked on the in-flight fetch before the leader may finish it.
    assert gate.until(CALLERS - 1), "the followers never blocked on the leader's fetch"
    hold.set()
    for thread in threads:
        thread.join(5)

    assert not failures, f"callers raised: {failures}"
    assert len(results) == CALLERS
    assert load.calls == 1, (
        f"{CALLERS} concurrent misses caused {load.calls} engine calls; "
        "the misses did not coalesce onto one in-flight fetch"
    )
    assert all(entry.value is SNAPSHOT for entry in results)


def test_the_coalescing_test_would_notice_a_second_fetch():
    """The counter above is real: two *sequential* misses do cost two calls."""
    clock = Clock()
    cache = SnapshotCache(ttl=2.0, clock=clock)
    load = Loader()
    cache.get(load)
    clock.advance(2.5)
    cache.get(load)
    assert load.calls == 2


def test_followers_take_the_leaders_failure_rather_than_retrying(
    monkeypatch: pytest.MonkeyPatch,
):
    """One outage must cost one engine call, not one per waiting request."""
    gate = FollowerGate()
    gate.install(monkeypatch)
    cache = SnapshotCache(ttl=60.0)
    hold = threading.Event()
    load = Loader(hold=hold, error=EngineUnavailableError("runner is down"))
    ready = threading.Barrier(CALLERS)
    raised: list[BaseException] = []

    def caller() -> None:
        ready.wait(5)
        try:
            cache.get(load)
        except BaseException as exc:  # noqa: BLE001 - carried to the test thread, which fails on it
            raised.append(exc)

    threads = [threading.Thread(target=caller) for _ in range(CALLERS)]
    for thread in threads:
        thread.start()
    assert gate.until(CALLERS - 1), "the followers never blocked on the leader's fetch"
    hold.set()
    for thread in threads:
        thread.join(5)

    assert load.calls == 1, f"an outage cost {load.calls} engine calls"
    assert len(raised) == CALLERS, "every caller must hear about the failure"
    assert all(isinstance(exc, EngineUnavailableError) for exc in raised)


# The age — the deliverable, not a side effect


def test_a_hit_reports_the_age_it_actually_has():
    """The age is measured off the clock, never assumed to be zero."""
    clock = Clock()
    cache = SnapshotCache(ttl=10.0, clock=clock)
    load = Loader()
    assert cache.get(load).age == 0.0
    clock.advance(4.0)
    entry = cache.get(load)
    assert entry.age == 4.0
    assert load.calls == 1, "a hit inside the TTL must not touch the engine"


def test_the_value_is_refetched_once_the_ttl_has_passed():
    clock = Clock()
    cache = SnapshotCache(ttl=2.0, clock=clock)
    load = Loader()
    cache.get(load)
    clock.advance(1.9)
    assert load.calls == 1
    clock.advance(0.2)
    assert cache.get(load).age == 0.0
    assert load.calls == 2


# Failure policy — stale but honest, never "fine and 0s old"


def test_a_failed_refresh_keeps_serving_the_old_value_with_a_growing_age():
    """The one thing a cache must never do is re-stamp a failure as fresh."""
    clock = Clock()
    cache = SnapshotCache(ttl=2.0, clock=clock)
    load = Loader()
    cache.get(load)

    clock.advance(30.0)
    load.error = EngineUnavailableError("runner is down")
    entry = cache.get(load)
    assert entry.value is SNAPSHOT, "a stale value beats no dashboard"
    assert entry.age == 30.0, "the age is the truth about the value, not the fetch"
    assert isinstance(entry.error, EngineUnavailableError)

    clock.advance(30.0)
    later = cache.get(load)
    assert later.age == 60.0, "an outage must make the age grow, not reset"
    assert load.calls == 3, "each request after a failure retries once"


def test_a_first_fetch_that_fails_surfaces_rather_than_inventing_a_herd():
    """With nothing to fall back on the error is the answer."""
    cache = SnapshotCache(ttl=2.0)
    load = Loader(error=EngineUnavailableError("runner is down"))
    with pytest.raises(EngineUnavailableError):
        cache.get(load)


def test_a_recovered_refresh_clears_the_failure_and_resets_the_age():
    clock = Clock()
    cache = SnapshotCache(ttl=2.0, clock=clock)
    load = Loader()
    cache.get(load)
    clock.advance(10.0)
    load.error = EngineUnavailableError("runner is down")
    assert cache.get(load).error is not None
    clock.advance(10.0)
    load.error = None
    entry = cache.get(load)
    assert entry.error is None
    assert entry.age == 0.0
