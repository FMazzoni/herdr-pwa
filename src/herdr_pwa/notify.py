"""The notify decision: which status changes buzz the phone, which clear it, and what the one
herd notification says. Pure apart from the injected ledgers; the polling loop lives elsewhere.
"""

import logging
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Final, Protocol, Self

from herdr_pwa.clock import Clock
from herdr_pwa.decisions import NO_OVERLAY, Overlay, read_status
from herdr_pwa.engine import AgentStatus, PaneId, SessionId
from herdr_pwa.notify_state import Interrupts, SeenLedger, StatusLedger
from herdr_pwa.push import (
    EVENT_BLOCKED,
    EVENT_CLEAR,
    EVENT_DONE,
    MAX_BODY_CHARS,
    DeliveryLog,
    Notice,
    PushEvent,
    sanitise,
)
from herdr_pwa.settings import Settings

log = logging.getLogger(__name__)

IDLE: Final = "idle"
WORKING: Final = "working"
STATUSES: Final = frozenset({IDLE, WORKING, EVENT_BLOCKED, EVENT_DONE})
NOTIFYING: Final[frozenset[PushEvent]] = frozenset({EVENT_BLOCKED, EVENT_DONE})
# Keeps the address key space from ever meeting the session one.
PANE_KEY: Final = "pane:"
MAX_NAME_CHARS: Final = 40
_ABSENT: Final = "\x00absent"
# The verb goes first so a long agent title cannot truncate it away.
_VERB: Final = MappingProxyType({EVENT_BLOCKED: "needs you", EVENT_DONE: "finished"})


class AgentView(Protocol):
    """The fields of one herdr agent the notifier reads; the engine's agent satisfies it."""

    @property
    def session(self) -> SessionId: ...

    @property
    def target(self) -> PaneId: ...

    @property
    def status(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def workspace(self) -> str: ...

    @property
    def project(self) -> str: ...

    @property
    def state_change_seq(self) -> int: ...


@dataclass(frozen=True, slots=True)
class Change:
    """One agent, one decision; ``replaces`` is the key it moved off without changing state."""

    session: str
    event: PushEvent
    agent: AgentView | None = None
    replaces: str = ""


@dataclass(frozen=True, slots=True)
class _Shown:
    """An agent as the phone shows it: the same fields, its status after the read rule."""

    session: SessionId
    target: PaneId
    status: str
    title: str
    workspace: str
    project: str
    state_change_seq: int


def _shown(agent: AgentView, overlay: Overlay) -> AgentView:
    # The rule is the feed's own: a stall needs you, a finish the phone has already shown is idle.
    status = read_status(AgentStatus.coerce(agent.status), agent.session, overlay)
    if status.value == str(agent.status):
        return agent
    return _Shown(
        agent.session,
        agent.target,
        status.value,
        agent.title,
        agent.workspace,
        agent.project,
        agent.state_change_seq,
    )


def _status_of(agent: AgentView) -> str:
    # A status this build cannot read is never recorded and never fired on.
    status = str(agent.status)
    return status if status in STATUSES else ""


def ledger_key(agent: AgentView) -> str:
    """The agent's session, or ``pane:<target>`` while it has none yet."""
    if agent.session:
        return str(agent.session)
    return f"{PANE_KEY}{agent.target}" if agent.target else ""


def statuses_of(agents: Sequence[AgentView]) -> dict[str, str]:
    """What the ledger records for this tick: key to status, unknowns dropped."""
    out: dict[str, str] = {}
    for agent in agents:
        key, status = ledger_key(agent), _status_of(agent)
        if key and status:
            out[key] = status
    return out


def _event_for(was: str, status: str, *, unread: bool) -> PushEvent | None:
    if status in NOTIFYING:
        return status
    if status == IDLE and was == WORKING:
        # herdr's done means the desk has not looked; a pane on screen settles to idle instead.
        return EVENT_DONE if unread else None
    if was in NOTIFYING or (status == WORKING and was == IDLE):
        return EVENT_CLEAR
    return None


def _inherited(before: Mapping[str, str], agent: AgentView, key: str) -> tuple[str, str]:
    if key in before or not agent.session:
        return before.get(key, _ABSENT), ""
    address = f"{PANE_KEY}{agent.target}" if agent.target else ""
    if address and address in before:
        return before[address], address
    return _ABSENT, ""


def _decide(
    before: Mapping[str, str], agent: AgentView, unread: Collection[str]
) -> tuple[tuple[str, ...], Change | None]:
    key, status = ledger_key(agent), _status_of(agent)
    if not key or not status:
        return (), None
    was, succeeds = _inherited(before, agent, key)
    # Both keys count as present, so the sweep never retracts an agent that merely got a session.
    keys = (key, succeeds) if succeeds else (key,)
    if (not succeeds and was == status) or (was == _ABSENT and status not in NOTIFYING):
        return keys, None
    event = _event_for("" if was == _ABSENT else was, status, unread=key in unread)
    if event is None:
        return keys, None
    return keys, Change(session=key, event=event, agent=agent, replaces=succeeds)


def transitions(
    before: Mapping[str, str], agents: Sequence[AgentView], *, unread: Collection[str] = ()
) -> list[Change]:
    """Fire into blocked or done (or an unread finish); clear everything else that was owed."""
    changes: list[Change] = []
    present: set[str] = set()
    for agent in agents:
        keys, change = _decide(before, agent, unread)
        present.update(keys)
        if change is not None:
            changes.append(change)
    changes.extend(
        Change(session=key, event=EVENT_CLEAR)
        for key, was in before.items()
        if key not in present and was in NOTIFYING
    )
    return changes


def notification_text(
    agent: AgentView, event: PushEvent, question: str = ""
) -> tuple[str, str, str]:
    """Title, body and fallback body for one agent; sanitising happens in the payload."""
    title = f"{_VERB.get(event, event)}: {agent.title or agent.project or 'agent'}"
    where = " · ".join(part for part in (agent.workspace, agent.project) if part)
    if event == EVENT_BLOCKED:
        body = f"{where} — {question}" if where and question else (question or where)
        return title, body, where or "waiting for an answer"
    return title, where, "no workspace"


@dataclass(frozen=True, slots=True)
class Alert:
    """One agent outstanding right now."""

    session: str
    event: PushEvent
    agent: AgentView | None = None


@dataclass(frozen=True, slots=True)
class Summary:
    """Everything outstanding, reduced to what the one notification must claim."""

    event: PushEvent
    session: str
    names: tuple[str, ...]
    blocked: int
    done: int


@dataclass(frozen=True, slots=True)
class Slot:
    """What the single herd notification must become; ``summary is None`` means retract it."""

    summary: Summary | None
    buzz: bool
    alerts: tuple[Alert, ...] = ()


def _name_of(alert: Alert) -> str:
    agent = alert.agent
    raw = alert.session if agent is None else (agent.title or agent.project or "agent")
    return sanitise(raw, limit=MAX_NAME_CHARS) or "an agent"


def _ordered(outstanding: Mapping[str, Alert]) -> tuple[Alert, ...]:
    # Urgency first, then arrival order.
    ranked = sorted(
        enumerate(outstanding.values()), key=lambda p: (p[1].event != EVENT_BLOCKED, p[0])
    )
    return tuple(alert for _, alert in ranked)


def _summarise(outstanding: Mapping[str, Alert]) -> Summary | None:
    alerts = _ordered(outstanding)
    if not alerts:
        return None
    blocked = sum(1 for one in alerts if one.event == EVENT_BLOCKED)
    return Summary(
        event=EVENT_BLOCKED if blocked else EVENT_DONE,
        session=alerts[0].session if len(alerts) == 1 else "",
        names=tuple(_name_of(one) for one in alerts),
        blocked=blocked,
        done=len(alerts) - blocked,
    )


def _herd_title(blocked: int, done: int) -> str:
    if blocked and done:
        return f"{blocked} {'needs' if blocked == 1 else 'need'} you, {done} finished"
    if blocked:
        return "1 agent needs you" if blocked == 1 else f"{blocked} agents need you"
    return "1 agent finished" if done == 1 else f"{done} agents finished"


def _name_list(names: Sequence[str], limit: int = MAX_BODY_CHARS) -> str:
    # Whole names then "+N more", never half of one.
    kept: list[str] = []
    for index, name in enumerate(names):
        rest = len(names) - index - 1
        tail = f" +{rest} more" if rest else ""
        if len(", ".join([*kept, name])) + len(tail) > limit:
            break
        kept.append(name)
    if len(kept) == len(names):
        return ", ".join(kept)
    if not kept:
        return f"{len(names)} agents"
    return ", ".join(kept) + f" +{len(names) - len(kept)} more"


def herd_text(summary: Summary) -> tuple[str, str, str]:
    """Title, body and fallback when more than one agent is outstanding: counts, then names."""
    return (
        _herd_title(summary.blocked, summary.done),
        _name_list(summary.names),
        "open the app to see the herd",
    )


class HerdSlot:
    """Debounce, coalesce and retract the one herd notification, as a function of a clock."""

    def __init__(self, *, clock: Clock, debounce: float = 0.0, desk_window: float = 0.0) -> None:
        self._clock: Clock = clock
        self._debounce: float = debounce
        self._desk_window: float = desk_window
        self._outstanding: dict[str, Alert] = {}
        self._held: dict[str, tuple[Alert, float]] = {}
        self._desk: dict[str, float] = {}
        self._buzzed: dict[str, float] = {}
        self._shown: Summary | None = None

    def note_desk(self, session: str) -> None:
        """This agent holds the globally focused pane; herdr offers no better presence signal."""
        if session:
            self._desk[session] = self._clock()

    def at_desk(self, session: str) -> bool:
        """Its pane was focused recently enough to infer somebody is sitting there."""
        if self._desk_window <= 0:
            return True
        seen = self._desk.get(session)
        return seen is not None and self._clock() - seen <= self._desk_window

    def next_due(self) -> float | None:
        """When the earliest held alert may be shown, so the loop can wake for it."""
        return min((at for _, at in self._held.values()), default=None)

    def forget(self, live: Mapping[str, str]) -> None:
        """Drop the marks of agents that are gone so neither map grows forever."""
        for book in (self._desk, self._buzzed):
            for session in [key for key in book if key not in live]:
                del book[session]

    def reset(self) -> None:
        """Forget everything: with nobody subscribed no lock screen holds anything."""
        self._outstanding.clear()
        self._held.clear()
        self._buzzed.clear()
        self._shown = None

    def restore(self, alerts: Sequence[Alert]) -> None:
        """Seed from the ledger: what the phone already holds is not news to re-send."""
        self._outstanding = {one.session: one for one in alerts}
        self._shown = _summarise(self._outstanding)

    def update(self, changes: Sequence[Change] = ()) -> Slot | None:
        """Apply the changes, release what is due, and return the slot only if it moved."""
        arrived = {name for change in changes if (name := self._record(change))}
        arrived |= self._release()
        summary = _summarise(self._outstanding)
        if summary == self._shown:
            return None
        self._shown = summary
        buzz = summary is not None and self._may_buzz(arrived)
        return Slot(summary=summary, buzz=buzz, alerts=_ordered(self._outstanding))

    def _record(self, change: Change) -> str:
        if change.event == EVENT_CLEAR:
            # A held alert that resolves is dropped unsent; a clear is never debounced.
            for name in (change.replaces, change.session):
                if name:
                    _ = self._held.pop(name, None)
                    _ = self._outstanding.pop(name, None)
            return ""
        alert = Alert(session=change.session, event=change.event, agent=change.agent)
        if self._succeed(change, alert):
            return ""
        if change.session in self._outstanding:
            self._outstanding[change.session] = alert
            return ""
        if self._debounce > 0 and self.at_desk(change.session):
            self._held[change.session] = (alert, self._clock() + self._debounce)
            return ""
        self._outstanding[change.session] = alert
        return change.session

    def _succeed(self, change: Change, alert: Alert) -> bool:
        # The alert moves quietly onto the session it just acquired: no second buzz.
        prior = change.replaces
        if not prior or prior == change.session:
            return False
        held = self._held.pop(prior, None)
        if self._outstanding.pop(prior, None) is not None:
            self._outstanding[change.session] = alert
            return True
        if held is not None:
            self._held[change.session] = (alert, held[1])
            return True
        return False

    def _release(self) -> set[str]:
        now = self._clock()
        due = [name for name, (_, at) in self._held.items() if at <= now]
        for session in due:
            self._outstanding[session] = self._held.pop(session)[0]
        return set(due)

    def _may_buzz(self, arrived: set[str]) -> bool:
        buzz = False
        now = self._clock()
        for session in sorted(arrived):
            last = self._buzzed.get(session)
            if last is None or now - last >= self._debounce:
                self._buzzed[session] = now
                buzz = True
        return buzz


def notice_for(
    slot: Slot, question: Callable[[AgentView], str] = lambda _agent: ""
) -> Notice | None:
    """The notification a moved slot becomes, or None when the slot must be retracted."""
    summary = slot.summary
    if summary is None:
        return None
    alert = slot.alerts[0] if len(slot.alerts) == 1 else None
    if alert is None or alert.agent is None:
        title, body, fallback = herd_text(summary)
    else:
        asked = question(alert.agent) if alert.event == EVENT_BLOCKED else ""
        title, body, fallback = notification_text(alert.agent, alert.event, asked)
    return Notice(
        event=summary.event,
        title=title,
        body=body,
        agent=summary.session,
        fallback=fallback,
        quiet=not slot.buzz,
    )


@dataclass(frozen=True, slots=True)
class NotifyConfig:
    """The notifier's knobs, lifted out of Settings so tests need no environment."""

    debounce: float = 30.0
    desk_window: float = 60.0
    unseen_finish: bool = True
    quiet_managed: bool = True

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """The knobs as configured by ``HP_PUSH_*``."""
        return cls(
            debounce=settings.push_debounce,
            desk_window=settings.push_desk_window,
            unseen_finish=settings.push_unseen_finish,
            quiet_managed=settings.push_quiet_managed,
        )


@dataclass(frozen=True, slots=True)
class Hooks:
    """Optional collaborators; each one that is absent or raises costs a refinement, never a buzz."""

    seen: SeenLedger | None = None
    is_managed: Callable[[PaneId], bool] | None = None
    policy: Callable[[], tuple[Interrupts, float]] | None = None
    health: DeliveryLog | None = None


@dataclass(frozen=True, slots=True)
class Step:
    """One tick's decision: the changes, and the slot to send if it moved."""

    changes: tuple[Change, ...] = ()
    slot: Slot | None = None
    primed: bool = False


class Planner:
    """One notifier tick without the I/O: diff, filter, coalesce. The caller reads and sends."""

    def __init__(
        self,
        ledger: StatusLedger,
        config: NotifyConfig,
        *,
        clock: Clock = time.time,
        hooks: Hooks | None = None,
    ) -> None:
        self._ledger: StatusLedger = ledger
        self._config: NotifyConfig = config
        self._hooks: Hooks = hooks or Hooks()
        self._prior: dict[str, str] | None = None
        self._loaded: bool = False
        self._restored: bool = False
        self.slot: HerdSlot = HerdSlot(
            clock=clock, debounce=config.debounce, desk_window=config.desk_window
        )

    def reset(self) -> None:
        """Nobody is subscribed: keep no prior, so the tick after a subscribe primes."""
        self._prior = None
        self._loaded = False
        self._restored = False
        self.slot.reset()

    def step(
        self,
        agents: Sequence[AgentView],
        *,
        focused_pane: str = "",
        beside: Overlay = NO_OVERLAY,
    ) -> Step:
        """Decide one tick against the herd as just read, and what the app read beside it."""
        # Observed every tick, priming included, so a finish has a "before" to be unread against.
        self._observe(agents)
        # A stall or a dialog already standing when watching began is primed as blocked, not news.
        standing = replace(beside, unread=None)
        prior = self._prior_for(statuses_of([_shown(one, standing) for one in agents]))
        if prior is None:
            return Step(primed=True)
        unread = self._unread()
        # A finish read on the phone is idle here too, so the alert it raised is taken down.
        # A stall stays blocked through the retries that wake it, so it buzzes once per stop.
        overlay = replace(beside, unread=_read_map(agents, unread))
        agents = [_shown(one, overlay) for one in agents]
        current = statuses_of(agents)
        self._restore(prior, agents)
        found = transitions(prior, agents, unread=unread or frozenset())
        changes = self._permitted(self._quiet(found))
        if len(changes) < len(found) and self._hooks.health is not None:
            self._hooks.health.quieted(len(found) - len(changes))
        self._prior = current
        self.slot.note_desk(_focused_session(agents, focused_pane))
        self.slot.forget(current)
        self._ledger.save(current)
        return Step(tuple(changes), self.slot.update(changes))

    def retract(self) -> None:
        """Forget whatever is showing so a snooze acts at once; the caller sends the clear."""
        self.slot.reset()

    def _prior_for(self, current: Mapping[str, str]) -> dict[str, str] | None:
        if self._prior is not None:
            return self._prior
        if not self._loaded:
            self._loaded = True
            self._prior = self._ledger.load()
            if self._prior is not None:
                return self._prior
        # Cold start: record the world and send nothing about it.
        self._prior = dict(current)
        self._ledger.save(current)
        log.info("notifier primed on %d agents; nothing sent", len(current))
        return None

    def _restore(self, prior: Mapping[str, str], agents: Sequence[AgentView]) -> None:
        if self._restored:
            return
        self._restored = True
        known = {ledger_key(one): one for one in agents}
        alerts = [
            Alert(key, event, known.get(key)) for key, event in prior.items() if event in NOTIFYING
        ]
        self.slot.restore(alerts)

    def _seen(self) -> SeenLedger | None:
        return self._hooks.seen if self._config.unseen_finish else None

    def _observe(self, agents: Sequence[AgentView]) -> None:
        seen = self._seen()
        try:
            if seen is not None:
                seen.observe(agents)
        except Exception:
            log.exception("the seen ledger raised; only herdr's done will fire")

    def _unread(self) -> frozenset[str] | None:
        # None when nothing says what was read: then no finish is taken for read either.
        seen = self._seen()
        try:
            return None if seen is None else frozenset(seen.unread_sessions())
        except Exception:
            log.exception("the seen ledger raised; only herdr's done will fire")
            return None

    def _quiet(self, changes: list[Change]) -> list[Change]:
        reader = self._hooks.is_managed
        if reader is None or not self._config.quiet_managed:
            return changes
        return [one for one in changes if not _managed_finish(one, reader)]

    def _permitted(self, changes: list[Change]) -> list[Change]:
        policy = self._hooks.policy
        if policy is None:
            return changes
        try:
            interrupts, now = policy()
        except Exception:
            log.exception("the interrupt store raised; the phone is still told")
            return changes
        return [one for one in changes if interrupts.allows(one.event, now=now)]


def _read_map(
    agents: Sequence[AgentView], unread: frozenset[str] | None
) -> Mapping[str, bool] | None:
    # Every session was just observed, so one missing from the unread set has been read.
    if unread is None:
        return None
    return {str(one.session): one.session in unread for one in agents if one.session}


def _managed_finish(change: Change, is_managed: Callable[[PaneId], bool]) -> bool:
    # Only a finish is ever quieted: a managed agent may be blocked because its watcher died.
    if change.event != EVENT_DONE or change.agent is None:
        return False
    try:
        return is_managed(change.agent.target)
    except Exception:
        log.exception("the managed reader raised; the phone is still told")
        return False


def _focused_session(agents: Sequence[AgentView], focused_pane: str) -> str:
    if not focused_pane:
        return ""
    return next((ledger_key(one) for one in agents if str(one.target) == focused_pane), "")
