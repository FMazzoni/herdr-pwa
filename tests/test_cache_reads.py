"""What the cache does for a follower: it waits on the leader's read and takes its outcome."""

import threading
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from herdr_pwa.clock import MonoTime
from herdr_pwa.engine import (
    CachedSnapshot,
    EngineTimeout,
    EngineUnavailable,
    ReadAbandoned,
    Snapshot,
    SnapshotCache,
)
from tests._cache_support import FollowerGate, TestClock
from tests._jsonpath import obj

FIXTURE = Path(__file__).parent / "fixtures" / "snapshot.example.json"
_RAW = obj(TypeAdapter(JsonValue).validate_json(FIXTURE.read_text()), "result", "snapshot")
SNAPSHOT = Snapshot.from_raw(_RAW)

# The default snapshot TTL the app ships with.
TTL = 2.0


class Clock:
    """A monotonic clock a test can wind forward, so nothing sleeps for a TTL."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> MonoTime:
        return MonoTime(self.now)


def test_a_follower_blocks_for_the_leaders_read_and_never_reads_itself(  # noqa: PLR0915 - a threaded scenario whose steps only make sense in order
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The property the old test could not see, because it used two apps."""
    gate = FollowerGate()
    gate.install(monkeypatch)
    timer = TestClock()
    cache = SnapshotCache(ttl=TTL)
    hold = threading.Event()
    leading = threading.Event()
    calls: list[str] = []

    def slow_load() -> Snapshot:
        calls.append("read")
        leading.set()
        assert hold.wait(timeout=10), "the test never released the leader"
        return SNAPSHOT

    entries: dict[str, CachedSnapshot] = {}
    waited: list[float] = []

    def lead() -> None:
        entries["leader"] = cache.get(slow_load)

    def follow() -> None:
        started = timer()
        entries["follower"] = cache.get(slow_load)
        waited.append(timer() - started)

    leader = threading.Thread(target=lead, daemon=True)
    leader.start()
    assert leading.wait(timeout=10), "the leader never started its read"
    follower = threading.Thread(target=follow, daemon=True)
    follower.start()
    # The follower is blocked on the leader's fetch, and nothing it can do will make progress.
    assert gate.until(1), "the follower never blocked on the leader's fetch"
    timer.advance(0.25)
    assert entries.get("follower") is None, "the follower did not wait at all"
    hold.set()
    leader.join(timeout=10)
    follower.join(timeout=10)

    assert calls == ["read"], "the follower read for itself; nothing was coalesced"
    assert waited[0] >= 0.2, f"it returned in {waited[0]:.3f}s"
    assert entries["follower"].value is SNAPSHOT


def test_a_leader_that_runs_to_the_full_command_budget_takes_its_followers_with_it(  # noqa: PLR0915 - a threaded scenario whose steps only make sense in order
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worst case, stated as a number."""
    gate = FollowerGate()
    gate.install(monkeypatch)
    timer = TestClock()
    budget = 0.3
    readers = 8
    cache = SnapshotCache(ttl=TTL)

    def timing_out() -> Snapshot:
        assert gate.until(readers - 1), "the followers never blocked on the leader's fetch"
        timer.advance(budget)
        raise EngineTimeout("the runner did not answer in time")

    waits: list[float] = []
    raised: list[str] = []
    lock = threading.Lock()

    def reader() -> None:
        started = timer()
        try:
            cache.get(timing_out)
        except Exception as exc:  # noqa: BLE001 - the test asserts which type each reader got
            with lock:
                raised.append(type(exc).__name__)
                waits.append(timer() - started)

    threads = [threading.Thread(target=reader, daemon=True) for _ in range(readers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert raised == ["EngineTimeout"] * 8, raised
    assert max(waits) >= budget, "somebody did not wait for the read it rode on"
    assert max(waits) < budget * 4, "the wait is bounded by the leader's own budget"


def test_a_killed_leader_releases_its_followers_with_a_failure_attached(  # noqa: PLR0915 - a threaded scenario whose steps only make sense in order
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled leader must neither strand its followers nor hand them a clean stale value."""
    gate = FollowerGate()
    gate.install(monkeypatch)
    clock = Clock()
    cache = SnapshotCache(ttl=TTL, clock=clock)
    cache.get(lambda: SNAPSHOT)
    clock.now += 30.0  # fifteen TTLs, which is the number the finding measured
    leading = threading.Event()
    hold = threading.Event()

    def killed() -> Snapshot:
        leading.set()
        assert hold.wait(timeout=10), "the test never released the leader"
        raise KeyboardInterrupt("the worker was cancelled")

    entries: dict[str, CachedSnapshot] = {}

    def lead() -> None:
        with pytest.raises(KeyboardInterrupt):
            cache.get(killed, max_age=0.0)

    def follow() -> None:
        entries["entry"] = cache.get(killed, max_age=0.0)

    # Daemon threads: a stranded follower must fail this assertion, not hang the interpreter.
    leader = threading.Thread(target=lead, daemon=True)
    follower = threading.Thread(target=follow, daemon=True)
    leader.start()
    assert leading.wait(timeout=10), "the leader never started its read"
    follower.start()
    assert gate.until(1), "it was never a follower"
    assert entries.get("entry") is None, "it was never a follower"
    hold.set()
    follower.join(timeout=5)
    leader.join(timeout=5)

    assert not follower.is_alive(), "stranded on a fetch that will never publish"
    assert entries["entry"].age == pytest.approx(30.0), "the age is never invented"
    assert isinstance(entries["entry"].error, ReadAbandoned), (
        "a value 15x past its TTL, handed over with error=None"
    )
    assert isinstance(entries["entry"].error, EngineUnavailable), (
        "it must map to a 503 like every other unreadable herd, not to a 500"
    )


def test_a_follower_takes_the_error_of_the_fetch_it_actually_waited_on(  # noqa: PLR0915 - a threaded scenario whose steps only make sense in order
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second leader's failure, stamped while a follower wakes, is not the follower's."""
    gate = FollowerGate()
    gate.install(monkeypatch)
    clock = Clock()
    cache = SnapshotCache(ttl=TTL, clock=clock)
    cache.get(lambda: SNAPSHOT)
    clock.now += 1.0
    leading = threading.Event()
    hold = threading.Event()

    def mine() -> Snapshot:
        leading.set()
        assert hold.wait(timeout=10), "the test never released the leader"
        raise EngineTimeout("my leader")

    def theirs() -> Snapshot:
        raise EngineUnavailable("somebody else's failure")

    entries: dict[str, CachedSnapshot] = {}

    def follow() -> None:
        entries["entry"] = cache.get(mine, max_age=0.0)

    leader = threading.Thread(target=lambda: cache.get(mine, max_age=0.0), daemon=True)
    follower = threading.Thread(target=follow, daemon=True)
    leader.start()
    assert leading.wait(timeout=10), "the leader never started its read"
    follower.start()
    assert gate.until(1), "it was never a follower"
    # The follower stays held after it wakes, so a second leader can stamp the shelf first.
    gate.woken.clear()
    hold.set()
    leader.join(timeout=5)
    cache.get(theirs, max_age=0.0)
    gate.woken.set()
    follower.join(timeout=5)

    entry = entries["entry"]
    assert isinstance(entry.error, EngineTimeout), (
        "the follower was handed the second leader's failure"
    )
    assert "somebody else" not in str(entry.error)
