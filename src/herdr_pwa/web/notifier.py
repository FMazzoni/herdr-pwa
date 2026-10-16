"""The notifier loop: read the herd on a clock, let the planner decide, push what it says."""

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol, Self

from herdr_pwa.engine import (
    BlockedPrompt,
    EngineError,
    PaneId,
    SessionId,
    Snapshot,
    SnapshotCache,
    as_pane_id,
    as_session_id,
)
from herdr_pwa.managed import ManagedHerd
from herdr_pwa.notify import AgentView, Hooks, NotifyConfig, Planner, Slot, Step, notice_for
from herdr_pwa.notify_state import InterruptStore, SeenLedger, StatusLedger
from herdr_pwa.push import (
    Deliver,
    DeliveryLog,
    FanOut,
    PushError,
    SubjectError,
    Subscription,
    SubscriptionError,
    SubscriptionStore,
    VapidKeys,
    arm_sender,
    build_clear,
    build_payload,
    encode_payload,
    fan_out,
    load_or_create_vapid_keys,
)
from herdr_pwa.settings import Settings

log = logging.getLogger(__name__)

Clock = Callable[[], float]

# A hold that is due must still be looked at; this keeps a stale due time from spinning.
MIN_SLEEP: Final = 0.25


class HerdReader(Protocol):
    """The runner reads the notifier may make; nothing here can write to an agent."""

    def snapshot(self, *, timeout: float | None = None) -> Snapshot: ...

    def blocked_prompt(
        self,
        target: PaneId,
        *,
        session: SessionId = ...,
        lines: int | None = None,
        timeout: float | None = None,
    ) -> BlockedPrompt: ...

    def pane_pids(self, pane_id: str, *, timeout: float | None = None) -> tuple[int, ...]: ...


@dataclass(frozen=True, slots=True)
class HerdSource:
    """Where the notifier reads the herd: the runner, through the feed's shared cache."""

    reader: HerdReader
    cache: SnapshotCache


@dataclass(frozen=True, slots=True)
class PushStores:
    """Everything push keeps in the state dir: key, devices, preferences, seen and health."""

    keys: VapidKeys
    devices: SubscriptionStore
    interrupts: InterruptStore
    seen: SeenLedger
    health: DeliveryLog
    state_dir: Path

    @classmethod
    def open(cls, state_dir: Path, *, clock: Clock = time.time) -> Self:
        """Load or create the VAPID key; a corrupt one raises rather than orphaning devices."""
        return cls(
            keys=load_or_create_vapid_keys(state_dir, clock=clock),
            devices=SubscriptionStore(state_dir, clock=clock),
            interrupts=InterruptStore(state_dir, clock=clock),
            seen=SeenLedger(state_dir, clock=clock),
            health=DeliveryLog(state_dir, clock=clock),
            state_dir=state_dir,
        )


@dataclass(frozen=True, slots=True)
class Tick:
    """What one pass did, for tests and the status route."""

    devices: int = 0
    step: Step | None = None
    sent: FanOut | None = None
    error: str = ""


@dataclass(slots=True)
class LoopState:
    """The loop's own vital signs, read by the status route from another thread."""

    running: bool = False
    ticks: int = 0
    last_tick_at: float = 0.0
    read_error: str = ""


@dataclass(frozen=True, slots=True)
class LoopConfig:
    """The loop's knobs, lifted out of Settings so tests need no environment."""

    interval: float = 2.0
    ledger_max_age: float = 900.0
    notify: NotifyConfig = field(default_factory=NotifyConfig)

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """The knobs as configured by ``HP_PUSH_*``."""
        return cls(
            interval=settings.push_interval,
            ledger_max_age=settings.push_ledger_max_age,
            notify=NotifyConfig.from_settings(settings),
        )


class Notifier:
    """One planner, fed a fresh snapshot each tick and wired to every subscribed device."""

    def __init__(
        self,
        *,
        herd: HerdSource,
        stores: PushStores,
        deliver: Deliver,
        config: LoopConfig,
        clock: Clock = time.time,
    ) -> None:
        self._reader: HerdReader = herd.reader
        self._cache: SnapshotCache = herd.cache
        self._stores: PushStores = stores
        self._deliver: Deliver = deliver
        self._config: LoopConfig = config
        self._clock: Clock = clock
        self._managed: ManagedHerd = ManagedHerd(herd.reader)
        ledger = StatusLedger(stores.state_dir, max_age=config.ledger_max_age, clock=clock)
        hooks = Hooks(
            seen=stores.seen,
            is_managed=self._managed.is_managed,
            policy=stores.interrupts.current,
            health=stores.health,
        )
        self.planner: Planner = Planner(ledger, config.notify, clock=clock, hooks=hooks)
        # The loop thread ticks while a request thread may retract; the planner is not reentrant.
        self._lock: threading.Lock = threading.Lock()
        self.state: LoopState = LoopState()
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None

    def tick(self) -> Tick:
        """One pass: skip when nobody listens, else read, decide and send."""
        with self._lock:
            devices = self._devices()
            if not devices:
                # No prior is kept, so the tick after a subscribe primes instead of replaying.
                self.planner.reset()
                return Tick()
            snapshot = self._read()
            if snapshot is None:
                return Tick(devices=len(devices), error=self.state.read_error)
            step = self.planner.step(snapshot.agents, focused_pane=snapshot.focused_pane_id or "")
            self._managed.forget([agent.target for agent in snapshot.agents])
            self.state.ticks += 1
            self.state.last_tick_at = self._clock()
            if step.slot is None:
                return Tick(devices=len(devices), step=step)
            return Tick(devices=len(devices), step=step, sent=self._send(step.slot, devices))

    def retract(self) -> FanOut:
        """Take down whatever the phones show, so a snooze acts at once."""
        with self._lock:
            self.planner.retract()
            devices = self._devices()
            if not devices:
                return FanOut()
            return self._broadcast(build_clear(now=self._clock()), devices)

    def interval(self) -> float:
        """Seconds until the next tick: the cadence, or sooner when a held alert falls due."""
        due = self.planner.slot.next_due()
        if due is None:
            return self._config.interval
        return max(MIN_SLEEP, min(self._config.interval, due - self._clock()))

    async def run(self, stop: asyncio.Event) -> None:
        """Tick until ``stop`` is set; one bad tick never ends the loop."""
        self.state.running = True
        try:
            while not stop.is_set():
                try:
                    _ = await asyncio.to_thread(self.tick)
                except Exception:
                    log.exception("notifier tick failed")
                with contextlib.suppress(TimeoutError):
                    _ = await asyncio.wait_for(stop.wait(), timeout=self.interval())
        finally:
            self.state.running = False

    def start(self) -> None:
        """Run the loop as a task on the current event loop."""
        if self._task is not None:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self.run(self._stop), name="herdr-pwa-notifier")
        log.info("notifier reading the herd every %.1fs", self._config.interval)

    async def stop(self) -> None:
        """Let the tick in flight finish, end the loop, and keep what the phone has seen."""
        task, self._task = self._task, None
        if self._stop is not None:
            self._stop.set()
        if task is not None:
            await task
        self._stores.seen.flush()

    def _devices(self) -> list[Subscription]:
        try:
            return self._stores.devices.list_all()
        except SubscriptionError as exc:
            log.warning("cannot read the subscription store: %s", exc)
            self._stores.health.failed(f"subscription store: {exc}")
            return []

    def _read(self) -> Snapshot | None:
        # Never diff a snapshot older than one tick; the feed's cache window is its own business.
        try:
            entry = self._cache.get(self._reader.snapshot, max_age=self._config.interval)
        except EngineError as exc:
            self.state.read_error = f"{type(exc).__name__}: {exc}"
            return None
        if entry.error is not None:
            self.state.read_error = f"{type(entry.error).__name__}: {entry.error}"
            return None
        self.state.read_error = ""
        return entry.value

    def _question(self, agent: AgentView) -> str:
        try:
            prompt = self._reader.blocked_prompt(
                as_pane_id(agent.target), session=as_session_id(agent.session)
            )
        # A missing question costs wording, never the buzz.
        except Exception as exc:
            log.info("no question for %s: %s", agent.session, type(exc).__name__)
            return ""
        return prompt.question

    def _send(self, slot: Slot, devices: list[Subscription]) -> FanOut:
        notice = notice_for(slot, self._question)
        now = self._clock()
        payload = build_clear(now=now) if notice is None else build_payload(notice, now=now)
        return self._broadcast(payload, devices)

    def _broadcast(self, payload: dict[str, object], devices: list[Subscription]) -> FanOut:
        try:
            data = encode_payload(payload)
        except PushError as exc:
            self._stores.health.failed(f"payload: {exc}")
            return FanOut()
        return fan_out(
            data,
            devices,
            deliver=self._deliver,
            store=self._stores.devices,
            health=self._stores.health,
        )


@dataclass(frozen=True, slots=True)
class PushState:
    """Push as this app runs it: the stores, and the notifier or why there is none."""

    stores: PushStores
    notifier: Notifier | None
    reason: str = ""


def open_push(
    settings: Settings,
    *,
    reader: HerdReader,
    cache: SnapshotCache,
    deliver: Deliver | None = None,
    clock: Clock = time.time,
) -> PushState | None:
    """Open the push stores and arm the notifier; None when push is switched off."""
    if not settings.push_enabled:
        log.info("push is off (HP_PUSH_ENABLED=0): no key, no routes answer, no loop")
        return None
    stores = PushStores.open(settings.state_dir, clock=clock)
    if not settings.push_notifier:
        return PushState(stores, None, "the notifier is switched off (HP_PUSH_NOTIFIER=0)")
    if deliver is None:
        try:
            deliver = arm_sender(stores.keys, subject=settings.push_subject, ttl=settings.push_ttl)
        except SubjectError as exc:
            # Loud but not fatal: the phone can still subscribe, and the alerts page says why.
            log.error("push is not armed: %s", exc)  # noqa: TRY400 - the reason is the message
            return PushState(stores, None, str(exc))
    notifier = Notifier(
        herd=HerdSource(reader, cache),
        stores=stores,
        deliver=deliver,
        config=LoopConfig.from_settings(settings),
        clock=clock,
    )
    return PushState(stores, notifier)
