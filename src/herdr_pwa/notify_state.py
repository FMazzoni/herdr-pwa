"""What the notifier remembers across ticks and restarts: prior statuses, what the phone has
seen, and which kinds of alert the user lets through. Every file is 0600 in the state dir.
"""

import json
import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol, Self

from herdr_pwa import jsonio, statedir
from herdr_pwa.push import EVENT_BLOCKED, EVENT_DONE
from herdr_pwa.statedir import StateError

log = logging.getLogger(__name__)

LEDGER_FILENAME = "notify.json"
SEEN_FILENAME = "seen.json"
INTERRUPTS_FILENAME = "interrupts.json"
# A fat finger must not mute a whole day.
MAX_DND_SECONDS = 12 * 3600
SEEN_FLUSH_INTERVAL = 10.0
SEEN_MAX_ENTRIES = 512

Clock = Callable[[], float]


def _write(path: Path, document: Mapping[str, object]) -> None:
    _ = statedir.ensure_dir(path.parent)
    statedir.write_secret(path, json.dumps(document, indent=2) + "\n")


def _read_document(path: Path) -> dict[str, object] | None:
    try:
        text = statedir.read_text(path)
        document = None if text is None else jsonio.as_dict(jsonio.loads(text))
    except (StateError, json.JSONDecodeError) as exc:
        log.warning("cannot use %s (%s); starting from nothing", path, exc)
        return None
    if document is None or document.get("version") != 1:
        return None
    return document


class StatusLedger:
    """The last status each agent was seen in, on disk so a restart is not a buzz storm."""

    def __init__(
        self, state_dir: Path, *, max_age: float = 900.0, clock: Clock = time.time
    ) -> None:
        self.path: Path = state_dir / LEDGER_FILENAME
        self._max_age: float = max_age
        self._clock: Clock = clock

    def load(self) -> dict[str, str] | None:
        """The prior to diff against, or None for absent, corrupt or stale: prime instead."""
        document = _read_document(self.path)
        if document is None:
            return None
        updated_at = jsonio.as_float(document.get("updated_at"))
        # A status from an hour ago is one the user already dealt with at the desk.
        if updated_at is None or self._clock() - updated_at > self._max_age:
            return None
        statuses = jsonio.as_dict(document.get("statuses")) or {}
        return {key: value for key, value in statuses.items() if isinstance(value, str)}

    def save(self, statuses: Mapping[str, str]) -> None:
        """Write the prior for the next tick and the next process; never raises."""
        document = {"version": 1, "updated_at": round(self._clock(), 3), "statuses": dict(statuses)}
        try:
            _write(self.path, document)
        except (OSError, StateError) as exc:
            log.warning("cannot write %s: %s; a restart will prime", self.path, exc)


class SeenAgent(Protocol):
    """The fields the seen ledger reads off one agent."""

    @property
    def session(self) -> str: ...

    @property
    def state_change_seq(self) -> int: ...


@dataclass(frozen=True, slots=True)
class SeenRecord:
    """When an agent last moved, and when the phone last showed it."""

    seq: int
    active_at: float
    seen_at: float

    @property
    def unread(self) -> bool:
        """Something happened after the last time the phone looked."""
        return self.active_at > self.seen_at


def _seen_record(raw: object) -> SeenRecord | None:
    seq = jsonio.as_int(jsonio.child(raw, "seq"))
    active = jsonio.as_float(jsonio.child(raw, "active_at"))
    seen = jsonio.as_float(jsonio.child(raw, "seen_at"))
    if seq is None or active is None or seen is None:
        return None
    return SeenRecord(seq, active, seen)


class SeenLedger:
    """Which finished work the phone has not opened yet; herdr's done only covers the desk."""

    def __init__(self, state_dir: Path, *, clock: Clock = time.time) -> None:
        self.path: Path = state_dir / SEEN_FILENAME
        self._clock: Clock = clock
        self._lock: threading.Lock = threading.Lock()
        self._records: dict[str, SeenRecord] | None = None
        self._dirty: bool = False
        self._flushed_at: float = 0.0

    def observe(self, agents: Iterable[SeenAgent]) -> None:
        """Note every agent whose state counter moved as active now."""
        now = self._clock()
        with self._lock:
            records = self._loaded()
            for agent in agents:
                self._observe_one(records, agent, now)
            self._trim(records)
        self._maybe_flush()

    def _observe_one(self, records: dict[str, SeenRecord], agent: SeenAgent, now: float) -> None:
        if not agent.session:
            return
        record = records.get(agent.session)
        if record is None:
            records[agent.session] = SeenRecord(agent.state_change_seq, now, now)
        elif record.seq != agent.state_change_seq:
            records[agent.session] = SeenRecord(agent.state_change_seq, now, record.seen_at)
        else:
            return
        self._dirty = True

    def mark_seen(self, session: str) -> None:
        """The phone showed this agent to the user just now; sending it a prompt does not count."""
        if not session:
            return
        now = self._clock()
        with self._lock:
            records = self._loaded()
            record = records.get(session) or SeenRecord(0, now, now)
            records[session] = replace(record, seen_at=now)
            self._dirty = True
            self._trim(records)
        self._maybe_flush()

    def unread_sessions(self) -> set[str]:
        """Every session whose last activity postdates the phone's last look."""
        with self._lock:
            return {key for key, record in self._loaded().items() if record.unread}

    def flush(self) -> None:
        """Write now, debounce or not; called on shutdown."""
        with self._lock:
            if not self._dirty:
                return
            seen = {
                key: {"seq": one.seq, "active_at": one.active_at, "seen_at": one.seen_at}
                for key, one in self._loaded().items()
            }
            self._dirty = False
            self._flushed_at = self._clock()
        try:
            _write(self.path, {"version": 1, "seen": seen})
        except (OSError, StateError) as exc:
            log.warning("cannot write %s: %s; 'seen' is in memory only", self.path, exc)

    def _maybe_flush(self) -> None:
        if self._clock() - self._flushed_at >= SEEN_FLUSH_INTERVAL:
            self.flush()

    def _loaded(self) -> dict[str, SeenRecord]:
        if self._records is None:
            document = _read_document(self.path) or {}
            raw = jsonio.as_dict(document.get("seen")) or {}
            parsed = {key: _seen_record(value) for key, value in raw.items()}
            self._records = {key: one for key, one in parsed.items() if one is not None}
        return self._records

    def _trim(self, records: dict[str, SeenRecord]) -> None:
        excess = len(records) - SEEN_MAX_ENTRIES
        if excess <= 0:
            return
        oldest = sorted(records.items(), key=lambda item: item[1].active_at)
        for key, _ in oldest[:excess]:
            del records[key]
        self._dirty = True


@dataclass(frozen=True, slots=True)
class Interrupts:
    """Which kinds may interrupt, and until when finishes are snoozed; defaults blocked on, done off."""

    blocked: bool = True
    done: bool = False
    dnd_until: float = 0.0

    def snoozed(self, now: float) -> bool:
        """The do-not-disturb window is still in the future."""
        return self.dnd_until > now

    def allows(self, event: str, *, now: float) -> bool:
        """May this event be sent now? Only the standing blocked preference can silence blocked."""
        if event == EVENT_BLOCKED:
            # Do-not-disturb is deliberately not consulted: needs-you is the app's purpose.
            return self.blocked
        if event == EVENT_DONE:
            return self.done and not self.snoozed(now)
        # A clear retracts; suppressing one leaves a stale alert on the lock screen.
        return True

    @classmethod
    def parse(cls, raw: object) -> Self:
        """Defaults for whatever is missing or malformed."""
        blocked = jsonio.child(raw, "blocked")
        done = jsonio.child(raw, "done")
        until = jsonio.as_float(jsonio.child(raw, "dnd_until")) or 0.0
        return cls(
            blocked=blocked if isinstance(blocked, bool) else True,
            done=done if isinstance(done, bool) else False,
            dnd_until=max(0.0, until),
        )


class InterruptStore:
    """The preferences on the box at 0600, because one push fans out to every device."""

    def __init__(self, state_dir: Path, *, clock: Clock = time.time) -> None:
        self.path: Path = state_dir / INTERRUPTS_FILENAME
        self._clock: Clock = clock

    def load(self) -> Interrupts:
        """What is on disk, or the defaults; an unreadable store is not fatal."""
        document = _read_document(self.path)
        return Interrupts.parse(None if document is None else document.get("interrupts"))

    def save(self, interrupts: Interrupts) -> Interrupts:
        """Persist the preferences; raises StateError or OSError when they cannot be kept."""
        body = {
            "blocked": interrupts.blocked,
            "done": interrupts.done,
            "dnd_until": interrupts.dnd_until,
        }
        _write(self.path, {"version": 1, "interrupts": body})
        return interrupts

    def set_kinds(self, *, blocked: bool | None = None, done: bool | None = None) -> Interrupts:
        """Change which kinds may ever fire, leaving the snooze alone."""
        current = self.load()
        return self.save(
            replace(
                current,
                blocked=current.blocked if blocked is None else blocked,
                done=current.done if done is None else done,
            )
        )

    def snooze(self, seconds: float) -> Interrupts:
        """Start, or end at 0, a do-not-disturb window clamped to twelve hours."""
        span = min(max(0.0, seconds), float(MAX_DND_SECONDS))
        return self.save(replace(self.load(), dnd_until=self._clock() + span if span else 0.0))

    def current(self) -> tuple[Interrupts, float]:
        """The policy and the clock reading it is judged against, taken together."""
        return self.load(), self._clock()
