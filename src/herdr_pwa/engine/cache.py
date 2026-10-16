"""One fetched snapshot, shared by every concurrent reader, with its real age."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol, cast

from herdr_pwa.clock import Clock
from herdr_pwa.engine.model import Snapshot
from herdr_pwa.engine.transport import EngineUnavailable

log = logging.getLogger(__name__)


class CacheSettings(Protocol):
    """The settings surface this module reads, and the whole of it."""

    @property
    def snapshot_cache_ttl(self) -> float: ...


_CLOCK: Final = time.monotonic

_SNAPSHOT: Final = "snapshot"


class ReadAbandoned(EngineUnavailable):
    """The leader of a fetch died without publishing anything."""


@dataclass(frozen=True, slots=True)
class CachedValue[T]:
    """What a reader gets: the value, how old it truly is, and any failure."""

    value: T
    age: float
    error: Exception | None = None


type CachedSnapshot = CachedValue[Snapshot]


class _Shelf:
    """Everything known about **one** cached fact, and who is fetching it."""

    __slots__ = ("error", "fetched_at", "in_flight", "value")

    def __init__(self) -> None:
        self.value: object | None = None
        self.fetched_at: float = 0.0
        self.error: Exception | None = None
        self.in_flight: _Fetch | None = None


class _Fetch:
    """One in-flight read, and the outcome the leader hands its followers."""

    __slots__ = ("done", "entry", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.entry: CachedValue[object] | None = None
        self.error: Exception | None = None

    def publish(self, entry: CachedValue[object] | None, error: Exception | None) -> None:
        """Record the outcome, then release everyone waiting. In that order."""
        self.entry = entry
        self.error = error
        self.done.set()


class SnapshotCache:
    """One shared value per fact per app, refreshed lazily, with misses coalesced."""

    def __init__(self, *, ttl: float, clock: Clock = _CLOCK) -> None:
        """Build a cache with its TTL and its clock, both injected."""
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._shelves: dict[str, _Shelf] = {}

    @classmethod
    def from_settings(cls, settings: CacheSettings) -> SnapshotCache:
        """Build one from *these* settings. The only place the TTL is read."""
        return cls(ttl=settings.snapshot_cache_ttl)

    @property
    def ttl(self) -> float:
        """How long a fetched value is served before the next read refetches it."""
        return self._ttl

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}(ttl={self._ttl!r})"

    def get(self, load: Callable[[], Snapshot], *, max_age: float | None = None) -> CachedSnapshot:
        """The herd snapshot, fetched with `load` only if nobody else is."""
        return self.read(_SNAPSHOT, load, max_age=max_age)

    def read[T](
        self, key: str, load: Callable[[], T], *, max_age: float | None = None
    ) -> CachedValue[T]:
        """The current value of ``key``, fetching it with ``load`` if nobody is."""
        with self._lock:
            shelf = self._shelves.setdefault(key, _Shelf())
            hit = self._hit_locked(shelf, max_age)
            if hit is not None:
                return cast(CachedValue[T], hit)
            fetch = shelf.in_flight
            leader = fetch is None
            if fetch is None:
                fetch = shelf.in_flight = _Fetch()
        if not leader:
            return cast(CachedValue[T], self._follow(fetch))
        try:
            return cast(CachedValue[T], self._fetch(shelf, load, fetch))
        finally:
            # A BaseException (shutdown, cancelled worker) must not strand followers; no-op once published.
            self._abandon(shelf, fetch)

    def _fetch(
        self, shelf: _Shelf, load: Callable[[], object], fetch: _Fetch
    ) -> CachedValue[object]:
        """Perform the one fetch, publish the outcome, release the followers."""
        try:
            value = load()
        except Exception as exc:
            survivor = self._publish_failure(shelf, exc, fetch)
            if survivor is None:
                raise
            log.warning(
                "refresh failed (%s); still serving a value that is %.1fs old",
                type(exc).__name__,
                survivor.age,
            )
            return survivor
        return self._publish(shelf, value, fetch)

    def _publish(self, shelf: _Shelf, value: object, fetch: _Fetch) -> CachedValue[object]:
        """Store a good value, stamp it, and wake everyone waiting on it."""
        entry = CachedValue(value=value, age=0.0)
        with self._lock:
            shelf.value = value
            shelf.fetched_at = self._clock()
            shelf.error = None
            self._release_locked(shelf, fetch)
        fetch.publish(entry, None)
        return entry

    def _publish_failure(
        self, shelf: _Shelf, error: Exception, fetch: _Fetch
    ) -> CachedValue[object] | None:
        """Record the failure **without touching the stamp**, and wake the followers."""
        with self._lock:
            shelf.error = error
            self._release_locked(shelf, fetch)
            survivor = self._entry_locked(shelf)
        fetch.publish(survivor, error)
        return survivor

    def _abandon(self, shelf: _Shelf, fetch: _Fetch) -> None:
        """Release a fetch that ended without publishing anything."""
        with self._lock:
            if shelf.in_flight is not fetch:
                return
            shelf.error = ReadAbandoned("the read was abandoned before it finished")
            shelf.in_flight = None
            survivor = self._entry_locked(shelf)
            error = shelf.error
        log.warning("a read was abandoned before it published; followers released")
        fetch.publish(survivor, error)

    def _release_locked(self, shelf: _Shelf, fetch: _Fetch) -> None:
        """Give up leadership, but only if we still hold it. Call under the lock."""
        if shelf.in_flight is fetch:
            shelf.in_flight = None

    def _follow(self, fetch: _Fetch) -> CachedValue[object]:
        """Wait for the leader's fetch and take **its** outcome — never fetch again."""
        fetch.done.wait()
        if fetch.entry is not None:
            return fetch.entry
        if fetch.error is not None:
            raise fetch.error
        raise EngineUnavailable("the herd could not be read")

    def _entry_locked(self, shelf: _Shelf) -> CachedValue[object] | None:
        """The stored value with its real age, or ``None`` if nothing is stored."""
        if shelf.value is None:
            return None
        return CachedValue(
            value=shelf.value,
            age=self._clock() - shelf.fetched_at,
            error=shelf.error,
        )

    def _hit_locked(
        self, shelf: _Shelf, max_age: float | None = None
    ) -> CachedValue[object] | None:
        """The stored value, only while it is fresh enough for *this* caller."""
        entry = self._entry_locked(shelf)
        if entry is None or entry.age >= self._ttl:
            return None
        if max_age is not None and entry.age > max_age:
            return None
        return entry
