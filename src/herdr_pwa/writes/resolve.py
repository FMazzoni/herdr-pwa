"""Find the target of a write in a snapshot taken now, and record the attempt before sending."""

import logging
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Final, Protocol

from herdr_pwa.audit import AuditUnavailableError, Entry, Write
from herdr_pwa.engine import (
    Agent,
    EngineError,
    HerdrClient,
    PaneId,
    Presence,
    SessionId,
    Snapshot,
    Workspace,
)
from herdr_pwa.writes.outcome import Pacing
from herdr_pwa.writes.refusal import Refusal, Refused

log = logging.getLogger(__name__)


class AuditTrail(Protocol):
    """Where a write's attempt is recorded before it is made; ``audit.Recorder`` is one."""

    def record(self, write: Write, /) -> Entry:
        """Append the attempt and hand back the entry its outcome closes."""
        ...


@dataclass(frozen=True, slots=True)
class Writer:
    """What every write needs: the runner, the audit trail, and the clock its waits run on."""

    client: HerdrClient
    audit: AuditTrail
    pacing: Pacing = Pacing()  # noqa: RUF009 - frozen, so one shared default is safe


def snapshot_now(client: HerdrClient, *, verb: str) -> Snapshot | Refused:
    """A snapshot taken in this request; a failed refresh refuses, never falls back to a cache."""
    try:
        return client.snapshot()
    except EngineError as exc:
        log.warning("write refused: the herd could not be read: %s", exc)
        return Refused(Refusal.UNREACHABLE, verb, detail=str(exc))


def agent_now(
    client: HerdrClient,
    session: SessionId,
    *,
    expected_target: PaneId | None,
    verb: str,
) -> Agent | Refused:
    """The agent holding ``session`` right now, followed if it moved; gone refuses."""
    snap = snapshot_now(client, verb=verb)
    if isinstance(snap, Refused):
        return snap
    # A swapped id raises here (as_session_id / as_pane_id); it is never "not found".
    resolution = snap.resolve_session(session, expected_target=expected_target)
    if resolution.presence is Presence.GONE or resolution.agent is None:
        return Refused(Refusal.GONE, verb, detail=resolution.message)
    return resolution.agent


def workspace_now(
    client: HerdrClient, workspace_id: str, *, verb: str
) -> tuple[Workspace, Snapshot] | Refused:
    """The workspace as a snapshot taken now has it, with that snapshot."""
    snap = snapshot_now(client, verb=verb)
    if isinstance(snap, Refused):
        return snap
    found = next((w for w in snap.workspaces if w.workspace_id == workspace_id.strip()), None)
    if found is None or not workspace_id.strip():
        return Refused(Refusal.NO_WORKSPACE, verb)
    return found, snap


def attempt(audit: AuditTrail, write: Write, *, verb: str) -> Entry | Refused:
    """Record the attempt before the runner is asked; a log that cannot record refuses the write."""
    try:
        return audit.record(write)
    except AuditUnavailableError as exc:
        log.exception("%s refused at %s: it could not be recorded", write.endpoint, write.target)
        return Refused(Refusal.UNRECORDABLE, verb, detail=str(exc))


@dataclass
class InFlight:
    """Which agents, panes and workspaces have a write in flight, in this process."""

    _guard: threading.Lock = field(default_factory=threading.Lock)
    _held: set[str] = field(default_factory=set[str])

    @contextmanager
    def hold(self, key: str) -> Generator[bool]:
        """Claim ``key`` for one write; yields False, and claims nothing, if it is already held."""
        with self._guard:
            mine = key not in self._held
            self._held.add(key)
        try:
            yield mine
        finally:
            if mine:
                with self._guard:
                    self._held.discard(key)


# One per process: uvicorn runs sync routes on a thread pool, so two tabs are two threads here.
IN_FLIGHT: Final = InFlight()


def workspace_key(workspace_id: str) -> str:
    """The lock key of a workspace: its rename, its close, and every start into it take this one."""
    return f"workspace:{workspace_id.strip()}"


def one_at_a_time[T](key: str, verb: str, write: Callable[[], T]) -> T | Refused:
    """Run ``write`` unless another write to the same ``key`` is still in flight; then refuse."""
    # Refused rather than queued: the second tap was made against what the first one changes, so
    # its sender has to look again either way, and a queue would pin a pool thread for the watch.
    with IN_FLIGHT.hold(key) as mine:
        return write() if mine else Refused(Refusal.IN_FLIGHT, verb)
