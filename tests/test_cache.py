"""Cache tests: the two properties the cache exists for, proved rather than asserted.

Both defects this ticket names are the kind that pass every gate while being
false, so neither is checked by reading the code:

* **Coalescing.** ``test_concurrent_misses_coalesce_onto_one_fetch`` fires N
  threads at a *cold* cache simultaneously and counts engine calls. A test that
  called sequentially would pass against a plain TTL cache with no single-flight
  in it at all, which is exactly the bug — so the loader here blocks until every
  caller has arrived, and the assertion is ``calls == 1``.
* **Non-blocking.** ``test_a_slow_read_does_not_stall_a_request_served_from_cache``
  runs two apps on one event loop, makes one of them perform a slow engine read
  and *measures* how long the other one's cache hit takes to come back. The
  numbers are printed into the assertion messages so a failure says what it saw.

Nothing here talks to a runner or opens a socket: the fake counts calls and
returns a recorded snapshot, which is also what makes the call count meaningful.
"""

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

try:
    from httpx2 import ASGITransport, AsyncClient
except Exception as exc:  # pragma: no cover - depends on the installed env
    pytest.skip(
        f"the concurrency tests need httpx2 in [dependency-groups].dev: {exc}",
        allow_module_level=True,
    )

from conftest import LOOPBACK_URL, NOT_SERVING  # noqa: E402
from herdr_pwa.engine import (  # noqa: E402
    CachedSnapshot,
    CacheSettings,
    EngineUnavailable,
    Snapshot,
    SnapshotCache,
    get_client,
)
from herdr_pwa.server import create_app  # noqa: E402
from herdr_pwa.settings import Settings, UiSettings

#: The login these tests act as when a request carries no identity header. That
#: is ``assume_login``'s job now; ``dev_mode`` is ergonomics only and
#: waives nothing, so it no longer appears here.
ASSUMED = "operator@example.com"

FIXTURE = Path(__file__).parent / "fixtures" / "snapshot.example.json"
SNAPSHOT = Snapshot.from_raw(json.loads(FIXTURE.read_text())["result"]["snapshot"])

#: A second, distinguishable herd, so "app B served app A's value" is visible.
OTHER = SNAPSHOT.model_copy(update={"agents": ()})


class Clock:
    """A monotonic clock a test advances by hand, so ages need no sleeping."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Loader:
    """A counted stand-in for ``client.snapshot``. Optionally slow, or broken."""

    def __init__(
        self,
        snapshot: Snapshot = SNAPSHOT,
        *,
        delay: float = 0.0,
        error: Exception | None = None,
        hold: threading.Event | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._delay = delay
        self._hold = hold
        self.error = error
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, **_: object) -> Snapshot:
        with self._lock:
            self.calls += 1
        if self._hold is not None:
            # Blocks until the test says every caller has arrived, which is what
            # makes "concurrent" true rather than hoped for.
            assert self._hold.wait(5), "the test never released the fetch"
        if self._delay:
            time.sleep(self._delay)
        if self.error is not None:
            raise self.error
        return self._snapshot

    def snapshot(self, **kwargs: object) -> Snapshot:
        """The routes resolve a client and call this; the cache takes it as its loader."""
        return self(**kwargs)

    def blocked_prompt(self, *_: object, **__: object) -> object:
        """No pane text in these tests; the feed renders such a card regardless."""
        raise EngineUnavailable("no pane to read")


# --------------------------------------------------------------------------
# Coalescing — the property a sequential test cannot see
# --------------------------------------------------------------------------

CALLERS = 10


def test_concurrent_misses_coalesce_onto_one_fetch():
    """Ten simultaneous misses on a cold cache must cost exactly one engine call.

    This is the measured justification: ten concurrent reads of the
    same fact degraded from 3.2ms p50 to 108ms max on the real runner, because
    the transport holds one connection behind one lock and every extra caller
    queues on it. A plain TTL cache with no single-flight would still let all
    ten through here — they all miss at the same instant — so the count, taken
    while the first fetch is deliberately held open, is the whole test.
    """
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
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            failures.append(exc)

    threads = [threading.Thread(target=caller) for _ in range(CALLERS)]
    for thread in threads:
        thread.start()
    # Give every follower time to arrive and register on the in-flight fetch
    # before the one leader is allowed to finish it.
    time.sleep(0.2)
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
    """The counter above is real: two *sequential* misses do cost two calls.

    Without this, ``calls == 1`` could be passing because the loader is never
    called twice under any circumstances, which would make the headline test a
    tautology.
    """
    clock = Clock()
    cache = SnapshotCache(ttl=2.0, clock=clock)
    load = Loader()
    cache.get(load)
    clock.advance(2.5)
    cache.get(load)
    assert load.calls == 2


def test_followers_take_the_leaders_failure_rather_than_retrying():
    """One outage must cost one engine call, not one per waiting request."""
    cache = SnapshotCache(ttl=60.0)
    hold = threading.Event()
    load = Loader(hold=hold, error=EngineUnavailable("runner is down"))
    ready = threading.Barrier(CALLERS)
    raised: list[BaseException] = []

    def caller() -> None:
        ready.wait(5)
        try:
            cache.get(load)
        except BaseException as exc:  # noqa: BLE001 - that is the assertion
            raised.append(exc)

    threads = [threading.Thread(target=caller) for _ in range(CALLERS)]
    for thread in threads:
        thread.start()
    time.sleep(0.2)
    hold.set()
    for thread in threads:
        thread.join(5)

    assert load.calls == 1, f"an outage cost {load.calls} engine calls"
    assert len(raised) == CALLERS, "every caller must hear about the failure"
    assert all(isinstance(exc, EngineUnavailable) for exc in raised)


# --------------------------------------------------------------------------
# The age — the deliverable, not a side effect
# --------------------------------------------------------------------------


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


def test_ttl_comes_from_settings_and_nowhere_else():
    """No hardcoded number: the TTL is a configured field like everything else."""
    cache = SnapshotCache.from_settings(Settings(snapshot_cache_ttl=37.0))
    assert cache._ttl == 37.0  # noqa: SLF001


# --------------------------------------------------------------------------
# The configuration boundary
# --------------------------------------------------------------------------


def test_the_cache_declares_the_one_field_it_reads_and_no_more():
    """``from_settings`` took a whole ``Settings`` and read one field of it.

    That is the same hidden dependency, seen a second time: a config
    object is only as valid as its worst field, so a module that accepts the
    whole thing can be taken down by a field it has never heard of.
    """
    reads = {name for name in dir(CacheSettings) if not name.startswith("_")}
    assert reads == {"snapshot_cache_ttl"}


def test_a_ui_domain_alone_is_enough_to_build_the_cache(monkeypatch):
    """The narrowing made structural rather than asserted.

    ``HP_BIND_HOST=0.0.0.0`` is refused by ``ServerSettings``' loopback rule,
    so a whole ``Settings`` cannot even be built in this environment. The cache
    does not care and now cannot be made to: ``UiSettings`` has no
    ``bind_host`` to validate, and it satisfies ``CacheSettings`` on its own.
    """
    monkeypatch.setenv("HP_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("HP_SNAPSHOT_CACHE_TTL", "11.0")
    with pytest.raises(ValidationError):
        Settings()
    cache = SnapshotCache.from_settings(UiSettings())
    assert cache._ttl == 11.0  # noqa: SLF001
    assert not hasattr(UiSettings(), "bind_host")


# --------------------------------------------------------------------------
# Failure policy — stale but honest, never "fine and 0s old"
# --------------------------------------------------------------------------


def test_a_failed_refresh_keeps_serving_the_old_value_with_a_growing_age():
    """The one thing a cache must never do is re-stamp a failure as fresh."""
    clock = Clock()
    cache = SnapshotCache(ttl=2.0, clock=clock)
    load = Loader()
    cache.get(load)

    clock.advance(30.0)
    load.error = EngineUnavailable("runner is down")
    entry = cache.get(load)
    assert entry.value is SNAPSHOT, "a stale value beats no dashboard"
    assert entry.age == 30.0, "the age is the truth about the value, not the fetch"
    assert isinstance(entry.error, EngineUnavailable)

    clock.advance(30.0)
    later = cache.get(load)
    assert later.age == 60.0, "an outage must make the age grow, not reset"
    assert load.calls == 3, "each request after a failure retries once"


def test_a_first_fetch_that_fails_surfaces_rather_than_inventing_a_herd():
    """With nothing to fall back on the error is the answer. Never an empty feed."""
    cache = SnapshotCache(ttl=2.0)
    load = Loader(error=EngineUnavailable("runner is down"))
    with pytest.raises(EngineUnavailable):
        cache.get(load)


def test_a_recovered_refresh_clears_the_failure_and_resets_the_age():
    clock = Clock()
    cache = SnapshotCache(ttl=2.0, clock=clock)
    load = Loader()
    cache.get(load)
    clock.advance(10.0)
    load.error = EngineUnavailable("runner is down")
    assert cache.get(load).error is not None
    clock.advance(10.0)
    load.error = None
    entry = cache.get(load)
    assert entry.error is None
    assert entry.age == 0.0


# --------------------------------------------------------------------------
# One cache per app
# --------------------------------------------------------------------------


def build_app(loader: Loader, **settings_kwargs: object):
    """An app wired to a counted fake, exactly as ``tests/test_server.py`` does."""
    settings = Settings(assume_login=ASSUMED, **settings_kwargs)  # type: ignore
    app = create_app(settings, serve_probe=NOT_SERVING)
    app.dependency_overrides[get_client] = lambda: loader
    return app


async def _get(app: object, path: str) -> tuple[int, str]:
    transport = ASGITransport(app=app)  # type: ignore
    async with AsyncClient(transport=transport, base_url=LOOPBACK_URL) as http:
        response = await http.get(path)
        return response.status_code, response.text


def test_two_apps_do_not_share_a_cache():
    """The bug this design exists to avoid, proved rather than promised.

    A module-scoped ``@cache``d instance would make the second app serve the
    first app's herd — which is precisely what happened to the engine client
    before the fix, and it was invisible until someone looked.
    """
    first, second = Loader(SNAPSHOT), Loader(OTHER)
    one, two = build_app(first), build_app(second)

    async def both() -> tuple[str, str]:
        await _get(one, "/feed")
        await _get(two, "/feed")
        return (await _get(one, "/feed"))[1], (await _get(two, "/feed"))[1]

    body_one, body_two = asyncio.run(both())
    assert first.calls == 1 and second.calls == 1, "each app fetched once"
    assert SNAPSHOT.agents[0].session in body_one
    assert SNAPSHOT.agents[0].session not in body_two, (
        "the second app was served the first app's snapshot"
    )
    assert "no agents" in body_two


def test_each_app_uses_its_own_ttl():
    """Settings reach the cache, so two apps can genuinely disagree about it."""
    cached, uncached = Loader(), Loader()
    warm = build_app(cached, snapshot_cache_ttl=60.0)
    cold = build_app(uncached, snapshot_cache_ttl=0.001)

    async def poll() -> None:
        for _ in range(3):
            await _get(warm, "/feed")
            await _get(cold, "/feed")
            await asyncio.sleep(0.01)

    asyncio.run(poll())
    assert cached.calls == 1, "a 60s TTL must serve all three polls from one read"
    assert uncached.calls == 3, "a 1ms TTL must expire between polls"


# --------------------------------------------------------------------------
# Non-blocking — measured, not asserted in a comment
# --------------------------------------------------------------------------

#: How long the slow app's engine read takes. Long enough that a stalled event
#: loop is unmistakable, short enough not to slow the suite down.
SLOW_READ = 0.8

#: How long after the slow read *starts* the cache hit must have been answered.
#: Measured from that instant rather than from when the hit was issued: on a
#: blocked event loop the hit cannot even be issued until the read finishes, so
#: a stopwatch started at the hit would measure nothing and pass either way.
#: Verified — that was the first version of this test, and the ``async def``
#: mutation below did not turn it red.
HIT_BUDGET = 0.25


def test_a_slow_read_does_not_stall_a_request_served_from_cache():
    """The blocking-call-in-``async def`` hazard, measured on two live apps.

    Both apps run on one event loop. One is asked for a fragment whose value
    must be fetched, and its engine read blocks for ``SLOW_READ`` seconds. The
    other's cache is already warm, so its answer needs no engine at all — and it
    must come back *now*, not when the slow read finishes.

    The routes are ``def``, so Starlette runs them in its worker threadpool and
    the blocking read occupies one thread rather than the loop. Change ``feed``
    to ``async def`` and this test fails with the hit taking the full
    ``SLOW_READ``: that mutation was run, and it does.
    """
    slow, warm = Loader(delay=SLOW_READ), Loader()
    slow_app, warm_app = build_app(slow), build_app(warm)

    async def race() -> tuple[float, float]:
        await _get(warm_app, "/feed")  # warm the cache before the stopwatch

        started = time.perf_counter()
        blocked = asyncio.create_task(_get(slow_app, "/feed"))
        await asyncio.sleep(0.05)  # let the slow read actually get going

        status, _ = await _get(warm_app, "/feed")
        hit_at = time.perf_counter() - started
        assert status == 200

        await blocked
        return hit_at, time.perf_counter() - started

    hit_at, slow_took = asyncio.run(race())

    assert slow_took >= SLOW_READ, (
        f"the slow read only took {slow_took:.3f}s; it never blocked, so this test proves nothing"
    )
    assert hit_at < HIT_BUDGET, (
        f"a cache hit was answered {hit_at:.3f}s after a {SLOW_READ}s engine "
        f"read began (budget {HIT_BUDGET:.3f}s) — the engine call is stalling "
        "the event loop, so every other request waits on it"
    )
    assert warm.calls == 1, "the hit must not have reached the engine"


def test_a_failed_refresh_serves_the_old_cards_and_names_the_failure():
    """End to end: a stale-but-honest fragment, never a blank herd or a lie.

    The value keeps rendering (an empty ``#feed-list`` is indistinguishable from
    "you have no agents", which is the one thing this UI must not imply), the
    age is the real one so the feed can call it stale, and ``HP-Degraded`` carries
    the failure class for a log or a prober.
    """
    load = Loader()
    app = build_app(load, snapshot_cache_ttl=0.001)

    async def outage() -> tuple[int, str, str | None]:
        await _get(app, "/feed")
        await asyncio.sleep(0.01)
        load.error = EngineUnavailable("runner is down")
        transport = ASGITransport(app=app)  # type: ignore
        async with AsyncClient(transport=transport, base_url=LOOPBACK_URL) as http:
            response = await http.get("/feed")
            return (
                response.status_code,
                response.text,
                response.headers.get("HP-Degraded"),
            )

    status, body, degraded = asyncio.run(outage())
    assert status == 200
    assert SNAPSHOT.agents[0].session in body, "the last good herd keeps rendering"
    assert degraded == "EngineUnavailable"
    assert load.calls == 2, "the failure was a real refresh attempt, not a hit"
