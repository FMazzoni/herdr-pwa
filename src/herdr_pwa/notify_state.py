"""What the notifier remembers across ticks and restarts: prior statuses, what the phone has
seen, and which kinds of alert the user lets through. Every file is 0600 in the state dir.
"""

import logging
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Annotated, Final, Literal, Protocol

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
)

from herdr_pwa import statedir
from herdr_pwa.clock import Clock
from herdr_pwa.ids import SessionId
from herdr_pwa.push import EVENT_BLOCKED, EVENT_DONE, PushEvent
from herdr_pwa.validation import or_default

log = logging.getLogger(__name__)

LEDGER_FILENAME: Final = "notify.json"
SEEN_FILENAME: Final = "seen.json"
INTERRUPTS_FILENAME: Final = "interrupts.json"
# A fat finger must not mute a whole day.
MAX_DND_SECONDS: Final = 12 * 3600
SEEN_FLUSH_INTERVAL: Final = 10.0
SEEN_MAX_ENTRIES: Final = 512


class _StateFile(BaseModel):
    """What every file here shares: a version this build reads, or the file is not used."""

    version: Literal[1]


def _write(path: Path, document: _StateFile) -> None:
    _ = statedir.ensure_dir(path.parent)
    statedir.write_secret(path, document.model_dump_json(indent=2) + "\n")


def _read[F: _StateFile](path: Path, model: type[F]) -> F | None:
    try:
        text = statedir.read_text(path)
        return None if text is None else model.model_validate_json(text)
    except (OSError, ValidationError) as exc:
        log.warning("cannot use %s (%s); starting from nothing", path, exc)
        return None


class _LedgerFile(_StateFile):
    """notify.json; a status that is not a string is dropped, the rest kept."""

    updated_at: StrictFloat
    statuses: Annotated[
        dict[str, Annotated[StrictStr | None, or_default(None)]], or_default({})
    ] = {}


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
        document = _read(self.path, _LedgerFile)
        # A status from an hour ago is one the user already dealt with at the desk.
        if document is None or self._clock() - document.updated_at > self._max_age:
            return None
        return {key: value for key, value in document.statuses.items() if value is not None}

    def save(self, statuses: Mapping[str, str]) -> None:
        """Write the prior for the next tick and the next process; never raises."""
        document = _LedgerFile(
            version=1, updated_at=round(self._clock(), 3), statuses=dict(statuses)
        )
        try:
            _write(self.path, document)
        except OSError as exc:
            log.warning("cannot write %s: %s; a restart will prime", self.path, exc)


class SeenAgent(Protocol):
    """The fields the seen ledger reads off one agent."""

    @property
    def session(self) -> str: ...

    @property
    def state_change_seq(self) -> int: ...


class SeenRecord(BaseModel):
    """When an agent last moved, and when the phone last showed it."""

    model_config = ConfigDict(frozen=True)

    seq: StrictInt
    active_at: StrictFloat
    seen_at: StrictFloat

    @property
    def unread(self) -> bool:
        """Something happened after the last time the phone looked."""
        return self.active_at > self.seen_at


class _SeenFile(_StateFile):
    """seen.json; a record missing any of its three numbers is dropped, the rest kept."""

    seen: Annotated[dict[str, Annotated[SeenRecord | None, or_default(None)]], or_default({})] = {}


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
        """Note every agent whose state counter moved as active now, and write it in due course."""
        self.note(agents)
        self._maybe_flush()

    def note(self, agents: Iterable[SeenAgent]) -> None:
        """As observe, in memory only: a read of the herd writes nothing; a write or exit keeps it."""
        now = self._clock()
        with self._lock:
            records = self._loaded()
            for agent in agents:
                self._observe_one(records, agent, now)
            self._trim(records)

    def _observe_one(self, records: dict[str, SeenRecord], agent: SeenAgent, now: float) -> None:
        if not agent.session:
            return
        record = records.get(agent.session)
        if record is None:
            records[agent.session] = SeenRecord(
                seq=agent.state_change_seq, active_at=now, seen_at=now
            )
        elif record.seq != agent.state_change_seq:
            records[agent.session] = record.model_copy(
                update={"seq": agent.state_change_seq, "active_at": now}
            )
        else:
            return
        self._dirty = True

    def mark_seen(self, session: SessionId) -> None:
        """The phone showed this agent to the user just now; sending it a prompt does not count."""
        if not session:
            return
        now = self._clock()
        with self._lock:
            records = self._loaded()
            record = records.get(session) or SeenRecord(seq=0, active_at=now, seen_at=now)
            records[session] = record.model_copy(update={"seen_at": now})
            self._dirty = True
            self._trim(records)
        self._maybe_flush()

    def unread_sessions(self) -> set[str]:
        """Every session whose last activity postdates the phone's last look."""
        with self._lock:
            return {key for key, record in self._loaded().items() if record.unread}

    def unread_map(self) -> dict[str, bool]:
        """Every session the ledger knows, and whether its last activity is still unread."""
        with self._lock:
            return {key: record.unread for key, record in self._loaded().items()}

    def flush(self) -> None:
        """Write now, debounce or not; called on shutdown."""
        with self._lock:
            if not self._dirty:
                return
            document = _SeenFile(version=1, seen=dict(self._loaded()))
            self._dirty = False
            self._flushed_at = self._clock()
        try:
            _write(self.path, document)
        except OSError as exc:
            log.warning("cannot write %s: %s; 'seen' is in memory only", self.path, exc)

    def _maybe_flush(self) -> None:
        if self._clock() - self._flushed_at >= SEEN_FLUSH_INTERVAL:
            self.flush()

    def _loaded(self) -> dict[str, SeenRecord]:
        if self._records is None:
            document = _read(self.path, _SeenFile) or _SeenFile(version=1)
            self._records = {key: one for key, one in document.seen.items() if one is not None}
        return self._records

    def _trim(self, records: dict[str, SeenRecord]) -> None:
        excess = len(records) - SEEN_MAX_ENTRIES
        if excess <= 0:
            return
        oldest = sorted(records.items(), key=lambda item: item[1].active_at)
        for key, _ in oldest[:excess]:
            del records[key]
        self._dirty = True


def _not_negative(until: float) -> float:
    return max(0.0, until)


class Interrupts(BaseModel):
    """Which kinds may interrupt, and until when finishes are snoozed; defaults blocked on, done off."""

    model_config = ConfigDict(frozen=True)

    blocked: Annotated[StrictBool, or_default(default=True)] = True
    done: Annotated[StrictBool, or_default(default=False)] = False
    dnd_until: Annotated[StrictFloat, or_default(0.0), AfterValidator(_not_negative)] = 0.0

    def snoozed(self, now: float) -> bool:
        """The do-not-disturb window is still in the future."""
        return self.dnd_until > now

    def allows(self, event: PushEvent, *, now: float) -> bool:
        """May this event be sent now? Only the standing blocked preference can silence blocked."""
        if event == EVENT_BLOCKED:
            # Do-not-disturb is deliberately not consulted: needs-you is the app's purpose.
            return self.blocked
        if event == EVENT_DONE:
            return self.done and not self.snoozed(now)
        # A clear retracts; suppressing one leaves a stale alert on the lock screen.
        return True


class _InterruptsFile(_StateFile):
    """interrupts.json; whatever is missing or malformed takes its default."""

    interrupts: Annotated[Interrupts, or_default(Interrupts())] = Interrupts()


class InterruptStore:
    """The preferences on the box at 0600, because one push fans out to every device."""

    def __init__(self, state_dir: Path, *, clock: Clock = time.time) -> None:
        self.path: Path = state_dir / INTERRUPTS_FILENAME
        self._clock: Clock = clock
        # The preference routes rewrite it from worker threads, each from what it read.
        self._lock: threading.Lock = threading.Lock()

    def load(self) -> Interrupts:
        """What is on disk, or the defaults; an unreadable store is not fatal."""
        document = _read(self.path, _InterruptsFile)
        return Interrupts() if document is None else document.interrupts

    def save(self, interrupts: Interrupts) -> Interrupts:
        """Persist the preferences; raises OSError when they cannot be kept."""
        _write(self.path, _InterruptsFile(version=1, interrupts=interrupts))
        return interrupts

    def set_kinds(self, *, blocked: bool | None = None, done: bool | None = None) -> Interrupts:
        """Change which kinds may ever fire, leaving the snooze alone."""
        with self._lock:
            current = self.load()
            return self.save(
                current.model_copy(
                    update={
                        "blocked": current.blocked if blocked is None else blocked,
                        "done": current.done if done is None else done,
                    }
                )
            )

    def snooze(self, seconds: float) -> Interrupts:
        """Start, or end at 0, a do-not-disturb window clamped to twelve hours."""
        span = min(max(0.0, seconds), float(MAX_DND_SECONDS))
        until = self._clock() + span if span else 0.0
        with self._lock:
            return self.save(self.load().model_copy(update={"dnd_until": until}))

    def current(self) -> tuple[Interrupts, float]:
        """The policy and the clock reading it is judged against, taken together."""
        return self.load(), self._clock()
