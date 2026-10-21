"""Find the target of a write in a snapshot taken now, and record the attempt before sending."""

import logging
import threading
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from herdr_pwa.audit import AuditUnavailableError, Entry, Write
from herdr_pwa.decisions import identity_doubt
from herdr_pwa.engine import (
    Agent,
    EngineError,
    HerdrClient,
    PaneId,
    Presence,
    SessionId,
    Snapshot,
    Workspace,
    holds_session,
    session_origin,
)
from herdr_pwa.managed import PROC
from herdr_pwa.writes.outcome import Pacing
from herdr_pwa.writes.refusal import Refusal, Refused

log = logging.getLogger(__name__)


class AuditTrail(Protocol):
    """Where a write's attempt is recorded before it is made; ``audit.Recorder`` is one."""

    def record(self, write: Write, /) -> Entry:
        """Append the attempt and hand back the entry its outcome closes."""
        ...


@dataclass(frozen=True, slots=True)
class Witness:
    """Where a session's holder is looked for: the runner, the session stores and /proc."""

    client: HerdrClient
    stores: tuple[str, ...] = ()
    proc: Path = PROC


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class Unsure:
    """The text each session was last sent with an unknown outcome, until something is sent."""

    _guard: threading.Lock = field(default_factory=threading.Lock)
    _text: dict[SessionId, str] = field(default_factory=dict[SessionId, str])

    def note(self, session: SessionId, text: str) -> None:
        """Remember that ``text`` may or may not have reached ``session``."""
        with self._guard:
            self._text[session] = text.strip()

    def pending(self, session: SessionId, text: str) -> bool:
        """Is ``text`` the one whose last send to ``session`` may already have landed?"""
        with self._guard:
            return bool(session) and self._text.get(session) == text.strip()

    def clear(self, session: SessionId) -> None:
        """A send to ``session`` was taken, so what came before it is settled by the pane."""
        with self._guard:
            _ = self._text.pop(session, None)


@dataclass(frozen=True, slots=True)
class WriteMemory:
    """What one app's writes remember between requests: the keys in flight, the unsure sends."""

    # Shared across the routes' thread pool, so two tabs are two threads on one set; a restart
    # forgets both, and the composer still says so.
    in_flight: InFlight = field(default_factory=InFlight)
    unsure: Unsure = field(default_factory=Unsure)


@dataclass(frozen=True, slots=True)
class Writer:
    """What every write needs: the runner, the audit trail, the clock its waits run on, the
    transcript roots a session's own store is looked up in (empty: each harness's default), the
    models the operator declared per kind, and the /proc a pane's process is looked up in.
    """

    client: HerdrClient
    audit: AuditTrail
    pacing: Pacing = Pacing()  # noqa: RUF009 - frozen, so one shared default is safe
    stores: tuple[str, ...] = ()
    declared: Mapping[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    proc: Path = PROC
    # Required: a fresh default would give a writer a memory no other request shares.
    memory: WriteMemory = field(kw_only=True)

    @property
    def witness(self) -> Witness:
        """Where this writer looks for who holds a session."""
        return Witness(self.client, self.stores, self.proc)


def snapshot_now(client: HerdrClient, *, verb: str) -> Snapshot | Refused:
    """A snapshot taken in this request; a failed refresh refuses, never falls back to a cache."""
    try:
        return client.snapshot()
    except EngineError as exc:
        log.warning("write refused: the herd could not be read: %s", exc)
        return Refused(Refusal.UNREACHABLE, verb, detail=str(exc))


def doubt(agent: Agent, witness: Witness) -> str:
    """Why *agent*'s reported session may belong to something else in its pane, or ``""``."""
    root = witness.stores or None
    said = identity_doubt(session_origin(agent, root=root), (agent.foreground_cwd, agent.cwd))
    # A folder renamed under a live agent leaves its store's path behind; the process the
    # pane's keys reach naming this session as its own outweighs where it started.
    if said and holds_session(agent, _leader(witness.client, agent), root=root, proc=witness.proc):
        log.info("%s holds %s although it started elsewhere", agent.target, agent.session)
        return ""
    return said


def _leader(client: HerdrClient, agent: Agent) -> int | None:
    try:
        return client.foreground_leader(agent.target)
    except EngineError:  # a runner that will not say only loses the proof, and the doubt stands
        log.debug("no process info for %s", agent.target, exc_info=True)
        return None


def agent_now(
    writer: Writer,
    session: SessionId,
    *,
    expected_target: PaneId | None,
    verb: str,
) -> Agent | Refused:
    """The agent holding ``session`` right now, followed if it moved; gone or doubted refuses."""
    snap = snapshot_now(writer.client, verb=verb)
    if isinstance(snap, Refused):
        return snap
    # A swapped id raises here (as_session_id / as_pane_id); it is never "not found".
    resolution = snap.resolve_session(session, expected_target=expected_target)
    if resolution.presence is Presence.GONE or resolution.agent is None:
        return Refused(Refusal.GONE, verb, detail=resolution.message)
    # The session check on the wire compares herdr's record with herdr's record; a pane whose
    # record was overwritten by a nested harness passes it, and the keys reach the real agent.
    said = doubt(resolution.agent, writer.witness)
    if said:
        log.warning("write refused at %s: %s", resolution.agent.target, said)
        return Refused(Refusal.DOUBTED_IDENTITY, verb, detail=said)
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


def workspace_key(workspace_id: str) -> str:
    """The lock key of a workspace: its rename, its close, and every start into it take this one."""
    return f"workspace:{workspace_id.strip()}"


def one_at_a_time[T](
    memory: WriteMemory, key: str, verb: str, write: Callable[[], T]
) -> T | Refused:
    """Run ``write`` unless another write to the same ``key`` is still in flight; then refuse."""
    # Refused rather than queued: the second tap was made against what the first one changes, so
    # its sender has to look again either way, and a queue would pin a pool thread for the watch.
    with memory.in_flight.hold(key) as mine:
        return write() if mine else Refused(Refusal.IN_FLIGHT, verb)
