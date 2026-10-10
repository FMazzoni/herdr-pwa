"""What the cache does for the reads that were never in it.

Three findings, one file, because they are three properties of the same object
and each was previously either unmeasured or measured by something that could
not fail:

Reads    ``server._prompts_for`` read one pane per blocked agent per request,
        in front of the cache rather than behind it. Blocked agents are what
        this product is *for*, so that is the primary path, not an edge.

Follower ``_follow`` waits with no timeout, so a slow read blocks the leader and
        every follower. That is correct, and the docstring claimed the
        opposite. Worse, the test that was supposed to measure it built **two
        separate apps**, so it exercised a cache hit and never a follower at
        all.

Leader   A leader killed by a ``BaseException`` released its followers a value
        long past the TTL with ``error=None`` — a clean 200 over a value 15x
        its TTL — and the whole ``_abandon`` path had no test: mutating the
        ``try/finally`` away left the suite green.

Every timing assertion here starts its stopwatch inside the thread being
measured, immediately before the call being measured. This project has
produced a test that started its clock at the wrong instant and stayed green
under mutation, and a timing test that cannot go red is worse than none.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

try:
    from fastapi.testclient import TestClient
except Exception as exc:  # pragma: no cover - depends on the installed env
    pytest.skip(f"TestClient needs httpx: {exc}", allow_module_level=True)

from conftest import NOT_SERVING  # noqa: E402
from herdr_pwa.engine import (  # noqa: E402
    BlockedPrompt,
    EngineTimeout,
    EngineUnavailable,
    Snapshot,
    TargetNotFound,
    get_client,
    get_snapshot_cache,
)
from herdr_pwa.engine.cache import (  # noqa: E402
    _SNAPSHOT,
    CachedValue,
    ReadAbandoned,
    SnapshotCache,
    _Fetch,
)
from herdr_pwa.server import DEGRADED_HEADER, create_app  # noqa: E402
from herdr_pwa.settings import Settings  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "snapshot.example.json"
SNAPSHOT = Snapshot.from_raw(json.loads(FIXTURE.read_text())["result"]["snapshot"])
BASE = SNAPSHOT.agents[0]
IDENTITY = {"Tailscale-User-Login": "operator@example.com"}

#: Six blocked agents, which is the herd the per-agent reads were measured on. Six because it
#: is an ordinary number of things to be waiting on and because 6 x 5s is the
#: half-minute the degraded path used to spend.
BLOCKED_HERD = SNAPSHOT.model_copy(
    update={
        "agents": tuple(
            BASE.model_copy(
                update={
                    "status": "blocked",
                    "session": f"5b609dba-d756-493a-a3fc-b4e09bf0a1{80 + i}",
                    "target": f"w652ae557d5b9c1:p{i}",
                }
            )
            for i in range(6)
        )
    }
)


def question(target: str) -> BlockedPrompt:
    """A question that names the pane it came from, so a mix-up is visible."""
    return BlockedPrompt(question=f"Proceed on {target}?", options=(), raw="$ probe")


class Clock:
    """A monotonic clock a test can wind forward, so nothing sleeps for a TTL."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class Herd:
    """A fake herdr that counts, can be slowed, and can be broken read by read."""

    def __init__(self, snapshot: Snapshot = BLOCKED_HERD) -> None:
        self.snapshot_value = snapshot
        self.snapshot_calls = 0
        self.agent_calls = 0
        self.prompt_calls = 0
        self.snapshot_ok = True
        self.pane_ok = True
        self.pane_delay = 0.0

    def snapshot(self, **_: object) -> Snapshot:
        self.snapshot_calls += 1
        if not self.snapshot_ok:
            raise EngineTimeout("the runner did not answer")
        return self.snapshot_value

    def blocked_prompt(self, target: object, **_: object) -> Any:
        self.prompt_calls += 1
        if self.pane_delay:
            time.sleep(self.pane_delay)
        if not self.pane_ok:
            raise EngineTimeout("the pane did not answer")
        return question(str(target))

    def agent(self, target: object, **_: object) -> Any:
        """The cheap drill-in read, counted apart from the snapshot.

        Apart, because the whole ticket is the RATIO: a drill-in tick used to
        cost a whole ``session.snapshot`` and now costs one ``agent.get``, and
        a fake that added them together could not show the difference. Broken
        by the SNAPSHOT switch, since both reads answer the same question --
        where is this agent -- off the same socket.
        """
        self.agent_calls += 1
        if not self.snapshot_ok:
            raise EngineTimeout("the runner did not answer")
        for candidate in self.snapshot_value.agents:
            if str(candidate.target) == str(target):
                return candidate
        raise TargetNotFound(f"no pane {target}")

    def mirror_read(self, *_: object, **__: object) -> str:
        return ""


def build(herd: Herd) -> tuple[TestClient, Clock]:
    """One app, one cache — which is what makes a follower reachable at all."""
    clock = Clock()
    settings = Settings(dev_mode=True)  # type: ignore
    app = create_app(settings, serve_probe=NOT_SERVING)
    cache = SnapshotCache(ttl=settings.snapshot_cache_ttl, clock=clock)
    app.dependency_overrides[get_client] = lambda: herd
    app.dependency_overrides[get_snapshot_cache] = lambda: cache
    return TestClient(app), clock


TTL = Settings(dev_mode=True).snapshot_cache_ttl  # type: ignore


# --------------------------------------------------------------------------
# The per-agent reads, which were in front of the cache
# --------------------------------------------------------------------------


def test_five_polls_of_six_blocked_agents_cost_six_pane_reads_not_thirty() -> None:
    """The measured defect, as the measurement.

    1 snapshot and **30** prompt reads was what the unmodified route did, and
    the reads are the expensive kind: they hit the pane, not the cheap
    snapshot. The number that matters is the second one, so it is asserted
    exactly rather than as "fewer than before".
    """
    herd = Herd()
    http, _ = build(herd)
    for _ in range(5):
        assert http.get("/feed", headers=IDENTITY).status_code == 200
    assert herd.snapshot_calls == 1
    assert herd.prompt_calls == len(BLOCKED_HERD.agents) == 6


def test_ten_phones_asking_at_once_cost_the_runner_one_read_per_agent() -> None:
    """The load case: the fan-out is what the cache exists to remove.

    Ten concurrent readers of a cold cache. Without coalescing this is sixty
    pane reads queueing on the transport's single lock at a command timeout
    each; with it, six.
    """
    herd = Herd()
    herd.pane_delay = 0.05  # wide enough that the ten really do overlap
    http, _ = build(herd)
    errors: list[BaseException] = []

    def poll() -> None:
        try:
            http.get("/feed", headers=IDENTITY)
        except BaseException as exc:  # pragma: no cover - reported, not raised
            errors.append(exc)

    threads = [threading.Thread(target=poll, daemon=True) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    assert herd.prompt_calls == 6, "one read per agent, not one per agent per phone"


def test_one_agents_question_is_never_served_for_another() -> None:
    """Keyed by session, and the key is the safety property rather than a detail.

    A pane id is positional and recycled; a question cached under one could
    outlive the agent and be shown over whoever inherited the pane. So the
    shelves are keyed by session, and this asserts the questions do not cross
    even when six of them are read in the same tick.
    """
    herd = Herd()
    http, _ = build(herd)
    body = http.get("/feed", headers=IDENTITY).text
    for agent in BLOCKED_HERD.agents:
        assert f"Proceed on {agent.target}?" in body


def test_a_degraded_snapshot_does_not_pay_for_reads_that_cannot_work() -> None:
    """The other half of the cost, and it is the worse half.

    When the snapshot is a survivor the runner is by definition not answering,
    so the request returns the survivor instantly and then used to spend up to
    one command timeout *per blocked agent* on reads that are all going to
    fail: six agents at 5 s is a 30 s feed poll for cards the app already has.
    The reads are skipped, and every blocked card says so rather than going
    quietly blank.
    """
    herd = Herd()
    http, clock = build(herd)
    http.get("/feed", headers=IDENTITY)
    reads_when_healthy = herd.prompt_calls
    herd.snapshot_ok = False
    herd.pane_ok = False
    herd.pane_delay = 0.5
    clock.now += TTL + 0.1

    started = time.monotonic()
    answer = http.get("/feed", headers=IDENTITY)
    elapsed = time.monotonic() - started

    assert answer.status_code == 200
    assert herd.prompt_calls == reads_when_healthy, "it asked anyway"
    assert elapsed < 0.5, f"the poll spent {elapsed:.2f}s on reads it knew would fail"
    assert answer.headers[DEGRADED_HEADER] == "EngineTimeout"
    assert "question unreadable" in answer.text


def test_the_window_in_which_a_lost_question_can_still_be_shown_is_the_ttl() -> None:
    """A cache is only honest if its staleness is bounded and it says when it is over.

    Inside the TTL a question is a hit and nothing is asked of the runner, which
    is the entire point. The claim being made here is the bound: one TTL later
    the read is attempted, it fails, and the card stops showing the question
    instead of keeping it indefinitely.
    """
    herd = Herd()
    http, clock = build(herd)
    assert "Proceed on" in http.get("/feed", headers=IDENTITY).text
    herd.pane_ok = False
    assert "Proceed on" in http.get("/feed", headers=IDENTITY).text, "still a hit"
    clock.now += TTL + 0.1
    body = http.get("/feed", headers=IDENTITY).text
    assert "Proceed on" not in body
    assert "question unreadable" in body


# --------------------------------------------------------------------------
# A follower, actually measured
# --------------------------------------------------------------------------


def test_a_follower_blocks_for_the_leaders_read_and_never_reads_itself() -> None:
    """The property the old test could not see, because it used two apps.

    Two apps means two caches means a cache *hit*, and a hit is not a follower.
    This is one cache, a leader held inside ``load``, and a follower whose own
    stopwatch starts in its own thread immediately before its own call. It
    asserts both halves: the follower really did wait (so the docstring's old
    "a slow read blocks only the one thread performing it" is false), and it
    really did coalesce (so the wait bought something).
    """
    cache = SnapshotCache(ttl=TTL)
    hold = threading.Event()
    leading = threading.Event()
    calls: list[str] = []

    def slow_load() -> Snapshot:
        calls.append("read")
        leading.set()
        assert hold.wait(timeout=10), "the test never released the leader"
        return SNAPSHOT

    outcome: dict[str, Any] = {}

    def lead() -> None:
        outcome["leader"] = cache.get(slow_load)

    def follow() -> None:
        started = time.monotonic()
        outcome["follower"] = cache.get(slow_load)
        outcome["waited"] = time.monotonic() - started

    leader = threading.Thread(target=lead, daemon=True)
    leader.start()
    assert leading.wait(timeout=10), "the leader never started its read"
    follower = threading.Thread(target=follow, daemon=True)
    follower.start()
    # The follower is now inside ``_follow``. Nothing it can do will make
    # progress until the leader publishes, and that is the finding.
    time.sleep(0.25)
    assert outcome.get("follower") is None, "the follower did not wait at all"
    hold.set()
    leader.join(timeout=10)
    follower.join(timeout=10)

    assert calls == ["read"], "the follower read for itself; nothing was coalesced"
    assert outcome["waited"] >= 0.2, f"it returned in {outcome['waited']:.3f}s"
    assert outcome["follower"].value is SNAPSHOT


def test_a_leader_that_runs_to_the_full_command_budget_takes_its_followers_with_it() -> None:
    """What the ticket asked for by name: the worst case, stated as a number.

    N followers each hold one of the ~40 Starlette threadpool slots for the
    whole of the leader's budget. The budget here is a stand-in measured in
    fractions of a second, but the shape is the one that matters: **N slots and
    one engine call**, rather than one slot and N engine calls. Every follower
    then receives the leader's failure rather than starting its own retry.
    """
    budget = 0.3
    cache = SnapshotCache(ttl=TTL)

    def timing_out() -> Snapshot:
        time.sleep(budget)
        raise EngineTimeout("the runner did not answer in time")

    waits: list[float] = []
    raised: list[str] = []
    lock = threading.Lock()

    def reader() -> None:
        started = time.monotonic()
        try:
            cache.get(timing_out)
        except Exception as exc:
            with lock:
                raised.append(type(exc).__name__)
                waits.append(time.monotonic() - started)

    threads = [threading.Thread(target=reader, daemon=True) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert raised == ["EngineTimeout"] * 8, raised
    assert max(waits) >= budget, "somebody did not wait for the read it rode on"
    assert max(waits) < budget * 4, "the wait is bounded by the leader's own budget"


# --------------------------------------------------------------------------
# The abandoned leader, and whose error a follower gets
# --------------------------------------------------------------------------


def test_a_killed_leader_releases_its_followers_with_a_failure_attached() -> None:
    """Both halves of the first finding, in the one scenario that produces them.

    The belt-and-braces ``finally`` had no test at all: mutating it out of
    ``get`` left 77 passing. What it prevents is a cancelled worker stranding
    every subsequent reader of that fact forever, so that half is asserted with
    a join timeout — a thread still alive at the end of the wait *is* the hang.

    The second half is what the follower is handed when it is released.
    Measured before the fix: released in 0.101s (the guard worked), with
    ``age=30.0`` on a 2.0s TTL and ``error=None`` — a value fifteen TTLs old,
    handed over as clean, which every caller in this app renders as an
    undegraded 200 with no ``HP-Degraded`` anywhere. Nothing was learned about
    the herd on an abandoned read, so what comes back now says exactly that.
    """
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

    outcome: dict[str, Any] = {}

    def lead() -> None:
        with pytest.raises(BaseException):  # noqa: B017,PT011 - that is the scenario
            cache.get(killed, max_age=0.0)

    def follow() -> None:
        started = time.monotonic()
        outcome["entry"] = cache.get(killed, max_age=0.0)
        outcome["waited"] = time.monotonic() - started

    # Daemon threads, because the failure mode under test is a thread that
    # never returns: a stranded non-daemon follower would hang the whole run at
    # interpreter exit instead of failing this one assertion.
    leader = threading.Thread(target=lead, daemon=True)
    follower = threading.Thread(target=follow, daemon=True)
    leader.start()
    assert leading.wait(timeout=10), "the leader never started its read"
    follower.start()
    time.sleep(0.1)
    assert outcome.get("entry") is None, "it was never a follower"
    hold.set()
    follower.join(timeout=5)
    leader.join(timeout=5)

    assert not follower.is_alive(), "stranded on a fetch that will never publish"
    assert outcome["entry"].age == pytest.approx(30.0), "the age is never invented"
    assert isinstance(outcome["entry"].error, ReadAbandoned), (
        "a value 15x past its TTL, handed over with error=None"
    )
    assert isinstance(outcome["entry"].error, EngineUnavailable), (
        "it must map to a 503 like every other unreadable herd, not to a 500"
    )


def test_a_follower_takes_the_error_of_the_fetch_it_actually_waited_on() -> None:
    """A finding the belt-and-braces comment implied was handled.

    ``_publish_failure`` cleared the in-flight marker under the lock and only
    then woke the waiters, so a caller entering that window became a *second*
    leader — and a follower of the first that went back to the shelf for the
    error picked up the second leader's. Cosmetic (it only mislabels
    ``HP-Degraded``) and still wrong, so the outcome now lives on the fetch
    rather than on the shelf.

    White-box on purpose: the race it describes is a window of a few
    instructions, and a test that tries to hit it by timing is a test that
    passes for the wrong reason most of the time. This asserts the structural
    property instead — a published fetch is immutable, and later shelf state
    cannot reach back into it.
    """
    cache = SnapshotCache(ttl=TTL, clock=Clock())
    cache.get(lambda: SNAPSHOT)

    # This thread's leader failed one way, and published that on its own fetch.
    mine = _Fetch()
    mine.publish(CachedValue(value=SNAPSHOT, age=1.0, error=EngineTimeout("my leader")), None)

    # A second leader then arrived, failed differently, and stamped the shelf.
    # A follower that went back to the shelf after waking would take this one.
    cache._shelves[_SNAPSHOT].error = EngineUnavailable(  # noqa: SLF001
        "somebody else's failure"
    )

    entry = cache._follow(mine)  # noqa: SLF001 - the point of the test
    assert isinstance(entry.error, EngineTimeout), (
        "the follower was handed the second leader's failure"
    )
    assert "somebody else" not in str(entry.error)


# --------------------------------------------------------------------------
# The drill-in stopped pulling the whole herd to look at one agent
# --------------------------------------------------------------------------
#
# The measurement, taken against the user's own running herd of eight agents on
# 2026-08-19 with `herdr api snapshot | wc -c` and `herdr agent get w2:p3 | wc
# -c`:
#
#     session.snapshot   12826 bytes
#     agent.get            532 bytes      ~24x smaller
#
# and the open Pane tab used to force TWO snapshots per 2s tick, because the
# viewport and the answer bar each resolved the session with `max_age=0.0`,
# which never takes a shelf hit. The tests below count the CALLS; the byte
# ratio above is why the calls that remain are also individually cheaper.


def mirror_path(target: str, session: str) -> str:
    return f"/agent/{session}/mirror?target={target}"


def test_a_drill_in_tick_costs_one_agent_read_and_no_snapshot() -> None:
    """The drill-in defect, as the measurement.

    Five polls of one agent's viewport used to cost five whole-herd snapshots
    on a page that is by definition looking at exactly one agent. They now cost
    five one-agent reads and zero snapshots.

    Zero, not "fewer": the cheap read either confirms the identity outright or
    it does not, and when it does there is nothing left for the snapshot to
    answer. A number that were merely smaller would mean the fallback was
    firing on the happy path.
    """
    herd = Herd()
    http, clock = build(herd)
    agent = herd.snapshot_value.agents[0]

    for _ in range(5):
        response = http.get(mirror_path(str(agent.target), str(agent.session)), headers=IDENTITY)
        assert response.status_code == 200
        # A real tick is 2s and the TTL is 2s, so each poll is a genuine miss.
        # Winding rather than sleeping, like every other test in this file.
        clock.now += TTL + 0.1

    assert herd.agent_calls == 5, "one cheap read per tick"
    assert herd.snapshot_calls == 0, (
        "a page looking at one agent must not pull the whole herd every tick"
    )


def test_the_feed_still_reads_the_whole_herd() -> None:
    """The feed is the one screen that genuinely wants every agent.

    Stated as a test because the obvious over-reach on the drill-in read is to make
    `agent.get` the default read everywhere, which would turn one snapshot into
    N per-agent calls and be strictly worse on the screen that matters most.
    """
    herd = Herd()
    http, clock = build(herd)

    http.get("/feed", headers=IDENTITY)
    clock.now += TTL + 0.1
    http.get("/feed", headers=IDENTITY)

    assert herd.snapshot_calls == 2, "the feed reads the herd, once per tick"
    assert herd.agent_calls == 0, "and never asks agent by agent"


def test_a_moved_agent_still_falls_back_to_the_whole_herd() -> None:
    """The cheap read cannot find an agent that is no longer at that address.

    So it must not be allowed to answer. This is the case the fallback exists
    for: `agent.get` on a stale pane id says "not that session" (or nothing at
    all), and only the snapshot knows where the identity went. A cheap read
    that answered GONE here would be the drill-in losing an agent that is
    alive.
    """
    herd = Herd()
    http, _ = build(herd)
    agent = herd.snapshot_value.agents[0]

    response = http.get(mirror_path("w652ae557d5b9c1:p99", str(agent.session)), headers=IDENTITY)

    assert herd.agent_calls == 1, "the cheap read was tried"
    assert herd.snapshot_calls == 1, "and the whole herd answered when it could not"
    assert response.status_code in {200, 404}


def test_a_drill_in_tick_is_honest_when_the_runner_is_gone() -> None:
    """A failed cheap read must not swallow the failure.

    The cheap read returning "no answer" is not an answer -- it falls through
    to the full read, which raises, which the fragment machinery turns into a
    swappable body carrying HP-Fragment. A poll that got a plain-text 503 here
    would freeze on its last-good viewport with its clock still running.
    """
    herd = Herd()
    http, _ = build(herd)
    agent = herd.snapshot_value.agents[0]
    herd.snapshot_ok = False

    response = http.get(mirror_path(str(agent.target), str(agent.session)), headers=IDENTITY)

    assert response.headers.get("HP-Fragment") == "1", "htmx must be able to swap it"
    assert "text/html" in response.headers["content-type"]
    assert herd.agent_calls == 1, "the cheap read was tried and failed"
    assert herd.snapshot_calls >= 1, "and the honest failure came from the full read"
