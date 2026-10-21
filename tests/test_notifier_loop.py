"""The notifier loop over a fake runner: what it reads, what it sends, and when it stays out."""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import JsonValue, TypeAdapter

from herdr_pwa.engine import (
    EngineSettings,
    EngineUnavailable,
    HerdrClient,
    SnapshotCache,
    engine_settings,
)
from herdr_pwa.notify import NotifyConfig
from herdr_pwa.protocol import WRITE_METHODS
from herdr_pwa.push import PUSH_LIMITS, DeliveryError, Subscription
from herdr_pwa.settings import Settings
from herdr_pwa.web.app import create_app
from herdr_pwa.web.notifier import HerdSource, LoopConfig, Notifier, PushStores, open_push
from herdr_pwa.web.seams import Seams
from herdr_pwa.web.stalls import Stalls
from tests.test_push import ENDPOINT, subscribed
from tests.waiting import until_async
from tests.webfakes import (
    READ,
    SNAPSHOT,
    WORKING_PANE,
    WORKING_SESSION,
    Clock,
    FakeWire,
    agents_of,
    read_frame,
    snapshot_frame,
)

# A tick reads afresh only once the shared cache's window has passed.
PAST_THE_CACHE = 10.0

OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


# Mutable: a fake the test drives by changing what it answers.
@dataclass(slots=True)
class Sender:
    """Records every payload instead of posting it; ``refuse`` answers as a push service would."""

    sent: list[tuple[str, dict[str, JsonValue]]] = field(
        default_factory=list[tuple[str, dict[str, JsonValue]]]
    )
    refuse: int | None = None
    # How long each send takes on the clock the fan-out budget is measured on.
    spends: tuple[Clock, float] | None = None

    def __call__(self, subscription: Subscription, data: str) -> None:
        if self.refuse is not None:
            raise DeliveryError("refused", status=self.refuse)
        self.sent.append((subscription.endpoint, OBJECT.validate_json(data)))
        if self.spends is not None:
            self.spends[0].now += self.spends[1]


# Mutable: the test's loop counts its ticks.
@dataclass(slots=True)
class Loop:
    """A notifier, its fake runner, clock, sender and stores."""

    notifier: Notifier
    wire: FakeWire
    clock: Clock
    # The notifier's own monotonic clock, apart from the rig's, so the two can disagree.
    elapsed: Clock
    sender: Sender
    stores: PushStores
    # herdr stamps each status change with a fresh counter; the seen ledger reads it.
    seq: int = 1000

    def set_status(self, status: str, pane: str = WORKING_PANE) -> None:
        frame = snapshot_frame()
        for agent in agents_of(frame):
            if agent.get("pane_id") == pane:
                self.seq += 1
                agent["agent_status"] = status
                agent["state_change_seq"] = self.seq
        self.wire.replies[SNAPSHOT] = frame
        self.clock.now += PAST_THE_CACHE

    def subscribe(self) -> None:
        _ = self.stores.devices.add(subscribed())


def build_loop(tmp_path: Path, notify: NotifyConfig | None = None) -> Loop:
    clock = Clock()
    wire = FakeWire({SNAPSHOT: snapshot_frame(), READ: read_frame("Proceed with the edit?")})
    engine = EngineSettings(socket_path=tmp_path / "no.sock", command_timeout=1.0)
    client = HerdrClient(engine, transport=wire, clock=clock)
    stores = PushStores.open(tmp_path / "state", clock=clock.wall)
    sender = Sender()
    elapsed = Clock()
    # No desk hold unless a test asks for one: a held alert would hide what the tick decided.
    chosen = notify or NotifyConfig(debounce=0.0, desk_window=0.0)
    config = LoopConfig(interval=2.0, notify=chosen)
    notifier = Notifier(
        herd=HerdSource(
            client, SnapshotCache(ttl=2.0, clock=clock), Stalls(tmp_path / "transcripts")
        ),
        stores=stores,
        config=config,
        seams=Seams(clock=elapsed, wall=clock.wall, deliver=sender),
    )
    return Loop(notifier, wire, clock, elapsed, sender, stores)


def test_with_nobody_subscribed_the_herd_is_not_even_read(tmp_path: Path) -> None:
    loop = build_loop(tmp_path)
    tick = loop.notifier.tick()
    assert tick.devices == 0
    assert loop.wire.calls == []
    assert loop.sender.sent == []


def test_the_first_tick_primes_silently(tmp_path: Path) -> None:
    loop = build_loop(tmp_path)
    loop.subscribe()
    loop.set_status("blocked")
    tick = loop.notifier.tick()
    assert tick.step is not None
    assert tick.step.primed
    assert loop.sender.sent == []


def test_an_agent_that_blocks_buzzes_every_device_with_its_question(tmp_path: Path) -> None:
    loop = build_loop(tmp_path)
    loop.subscribe()
    _ = loop.notifier.tick()
    loop.set_status("blocked")
    tick = loop.notifier.tick()
    assert tick.sent is not None
    assert tick.sent.sent == 1
    [(endpoint, payload)] = loop.sender.sent
    assert endpoint == ENDPOINT
    assert payload["event"] == "blocked"
    assert payload["agent"] == WORKING_SESSION
    assert payload["tag"] == "herd"
    assert payload["quiet"] is False
    assert str(payload["title"]).startswith("needs you")
    health = loop.stores.health.load()
    assert health is not None
    assert health.sent == 1


def test_resolving_the_block_takes_the_notification_down(tmp_path: Path) -> None:
    loop = build_loop(tmp_path)
    loop.subscribe()
    _ = loop.notifier.tick()
    loop.set_status("blocked")
    _ = loop.notifier.tick()
    loop.set_status("working")
    _ = loop.notifier.tick()
    assert [payload["event"] for _, payload in loop.sender.sent] == ["blocked", "clear"]


def test_an_unchanged_herd_sends_nothing_more(tmp_path: Path) -> None:
    loop = build_loop(tmp_path)
    loop.subscribe()
    _ = loop.notifier.tick()
    loop.set_status("blocked")
    _ = loop.notifier.tick()
    loop.clock.now += PAST_THE_CACHE
    _ = loop.notifier.tick()
    assert len(loop.sender.sent) == 1


def test_a_finish_is_quiet_by_default_because_done_is_off(tmp_path: Path) -> None:
    loop = build_loop(tmp_path)
    loop.subscribe()
    _ = loop.notifier.tick()
    loop.set_status("done")
    tick = loop.notifier.tick()
    assert tick.step is not None
    assert tick.step.changes == ()
    health = loop.stores.health.load()
    assert health is not None
    assert health.quieted == 1
    assert loop.sender.sent == []


def test_the_notifier_never_calls_a_write_method(tmp_path: Path) -> None:
    loop = build_loop(tmp_path)
    loop.subscribe()
    for status in ("working", "blocked", "done", "idle", "blocked"):
        loop.set_status(status)
        _ = loop.notifier.tick()
    assert loop.wire.calls
    assert not set(loop.wire.calls) & WRITE_METHODS


def test_a_runner_that_will_not_answer_skips_the_tick_and_says_why(tmp_path: Path) -> None:
    loop = build_loop(tmp_path)
    loop.subscribe()
    loop.wire.replies[SNAPSHOT] = EngineUnavailable("no socket")
    tick = loop.notifier.tick()
    assert tick.step is None
    assert "EngineUnavailable" in tick.error
    assert "EngineUnavailable" in loop.notifier.state.read_error


def test_a_device_the_push_service_retired_is_pruned(tmp_path: Path) -> None:
    loop = build_loop(tmp_path)
    loop.subscribe()
    _ = loop.notifier.tick()
    loop.sender.refuse = 410
    loop.set_status("blocked")
    tick = loop.notifier.tick()
    assert tick.sent is not None
    assert tick.sent.pruned == 1
    assert loop.stores.devices.list_all() == []


def test_retract_sends_the_clear_to_every_device(tmp_path: Path) -> None:
    loop = build_loop(tmp_path)
    loop.subscribe()
    sent = loop.notifier.retract()
    assert sent.sent == 1
    assert [payload["event"] for _, payload in loop.sender.sent] == ["clear"]


def test_a_held_alert_shortens_the_sleep_to_when_it_falls_due(tmp_path: Path) -> None:
    # The fixture's focused pane is the one that blocks, so the desk hold applies.
    loop = build_loop(tmp_path, NotifyConfig(debounce=30.0, desk_window=60.0))
    loop.subscribe()
    _ = loop.notifier.tick()
    assert loop.notifier.interval() == 2.0
    loop.set_status("blocked")
    _ = loop.notifier.tick()
    assert loop.sender.sent == []
    loop.clock.now += 29.0
    assert loop.notifier.interval() == 1.0
    loop.clock.now += 1.0
    _ = loop.notifier.tick()
    assert [payload["event"] for _, payload in loop.sender.sent] == ["blocked"]


def test_the_loop_ticks_until_stopped_and_survives_a_bad_tick(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    loop = build_loop(tmp_path)
    loop.subscribe()
    loop.wire.replies[SNAPSHOT] = RuntimeError("a bug in a tick")

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(loop.notifier.run(stop))
        # The bad tick has run and been logged; the loop is still going after it.
        failed = "notifier tick failed"
        await until_async(lambda: failed in caplog.messages or task.done(), 5.0)
        assert not task.done(), "a bad tick ended the loop"
        assert loop.notifier.state.running
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(scenario())
    assert loop.notifier.state.running is False


def test_push_is_not_armed_without_a_contact_and_says_so(tmp_path: Path) -> None:
    settings = Settings(state_dir=tmp_path / "state", socket_path=tmp_path / "no.sock")
    reader = HerdrClient(engine_settings(settings), transport=FakeWire())
    state = open_push(
        settings, HerdSource(reader, SnapshotCache(ttl=1.0), Stalls(tmp_path)), Seams()
    )
    assert state is not None
    assert state.notifier is None
    assert "HP_PUSH_SUBJECT" in state.reason


def test_the_app_starts_and_stops_the_loop_with_its_lifespan(tmp_path: Path) -> None:
    settings = Settings(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "no.sock",
        push_subject="mailto:operator@example.net",
        push_interval=0.05,
        public_host="testserver",
    )
    wire = FakeWire({SNAPSHOT: snapshot_frame()})

    def factory(engine: EngineSettings) -> HerdrClient:
        return HerdrClient(engine, transport=wire)

    sender = Sender()
    app = create_app(settings, Seams(client_factory=factory, deliver=sender))
    with TestClient(app, headers={"Tailscale-User-Login": "op@example.net"}) as http:
        status = OBJECT.validate_json(http.get("/push/status").text)
        assert (status["armed"], status["running"]) == (True, True)
    # Nobody subscribed, so the loop never read the herd.
    assert wire.calls == []


def test_the_fan_out_budget_runs_on_the_monotonic_clock_not_the_wall(tmp_path: Path) -> None:
    loop = build_loop(tmp_path)
    for n in range(2):
        _ = loop.stores.devices.add(subscribed(f"https://fcm.googleapis.com/fcm/send/{n}"))
    _ = loop.notifier.tick()
    loop.set_status("blocked")
    # The first send outlasts the budget on the monotonic clock while the wall stands still.
    loop.sender.spends = (loop.elapsed, PUSH_LIMITS.fan_out_budget + 1)
    tick = loop.notifier.tick()
    assert tick.sent is not None
    assert (tick.sent.sent, tick.sent.failed) == (1, 1)
    assert len(loop.sender.sent) == 1
