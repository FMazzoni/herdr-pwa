"""Typed shapes the rest of herdr-pwa renders from."""

import hashlib
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Self, cast

from pydantic import BaseModel, ConfigDict, computed_field

from herdr_pwa.ids import PaneId, SessionId, WorkspaceId
from herdr_pwa.protocol import EXPECTED_PROTOCOL as _EXPECTED_PROTOCOL

type _Raw = Mapping[str, object]


def _to_int(value: object) -> int:
    """``int(value)`` for whatever a JSON field holds, failing the way ``int`` would."""
    if isinstance(value, int | float | str | bytes | bytearray):
        return int(value)
    raise TypeError(
        f"int() argument must be a string or a real number, not {type(value).__name__!r}"
    )


def _records(value: object) -> list[_Raw]:
    """The objects of a JSON array; anything else in it is a malformed payload."""
    if not value:
        return []
    if not isinstance(value, Iterable):
        raise TypeError(f"{type(value).__name__!r} object is not iterable")
    found: list[_Raw] = []
    for item in cast(Iterable[object], value):
        if not isinstance(item, Mapping):
            raise TypeError(f"{type(item).__name__!r} object is not a JSON object")
        found.append(cast(_Raw, item))
    return found


def _panes(value: object) -> list[_Raw]:
    """The pane objects of a snapshot, skipping anything that is not one."""
    if not value:
        return []
    if not isinstance(value, Iterable):
        raise TypeError(f"{type(value).__name__!r} object is not iterable")
    return [cast(_Raw, p) for p in cast(Iterable[object], value) if isinstance(p, dict)]


def screen_digest(screen: str) -> str:
    """A change detector for one rendered terminal screen."""
    return hashlib.sha256(screen.encode("utf-8", "replace")).hexdigest()[:16]


EXPECTED_PROTOCOL = _EXPECTED_PROTOCOL


_PANE_SHAPE = re.compile(r"^[^\s:]+:[^\s:]+$")

_UUID_SHAPE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def as_session_id(value: object) -> SessionId:
    """Tag an untrusted string (a URL path segment, a notification payload) as identity."""
    text = str(value or "").strip()
    if _PANE_SHAPE.match(text):
        raise ValueError(
            f"{text!r} is a pane id (an address), not a session id (an identity); "
            "pane ids are recycled and must never be used to identify an agent"
        )
    return SessionId(text)


def as_pane_id(value: object) -> PaneId:
    """Tag an untrusted string as an address to drive *this tick*."""
    text = str(value or "").strip()
    if _UUID_SHAPE.match(text):
        raise ValueError(
            f"{text!r} is a session id (an identity), not a pane id (an address); "
            "resolve it against a fresh snapshot with Snapshot.resolve_session first"
        )
    return PaneId(text)


@dataclass(frozen=True, slots=True)
class Seat:
    """A pane to type at, and the session that must still hold it, since panes are recycled.

    ``session=None`` types at whoever holds the pane: only for a pane this same request created.
    """

    target: PaneId
    session: SessionId | None = None


class AgentStatus(StrEnum):
    """Lifecycle state of an agent."""

    IDLE = "idle"
    WORKING = "working"
    BLOCKED = "blocked"
    DONE = "done"
    UNKNOWN = "unknown"

    @classmethod
    def coerce(cls, value: object) -> "AgentStatus":
        """Map any value onto the enum, falling back to :attr:`UNKNOWN`."""
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError:
            return cls.UNKNOWN


_RANKS: dict[AgentStatus, int] = {
    AgentStatus.BLOCKED: 0,
    AgentStatus.DONE: 1,
    AgentStatus.WORKING: 2,
    AgentStatus.IDLE: 3,
    AgentStatus.UNKNOWN: 4,
}


def rank(status: object) -> int:
    """Urgency rank: blocked=0, done=1, working=2, idle=3, unknown=4 (lower = more urgent)."""
    return _RANKS[AgentStatus.coerce(status)]


class Workspace(BaseModel):
    """A runner workspace — the human-named grouping an agent lives in."""

    model_config = ConfigDict(frozen=True)

    workspace_id: WorkspaceId
    label: str = ""
    number: int | None = None
    status: AgentStatus = AgentStatus.UNKNOWN
    focused: bool = False
    pane_count: int = 0
    tab_count: int = 0

    @classmethod
    def from_raw(cls, raw: _Raw) -> Self:
        """Normalize one snapshot ``workspaces[]`` entry."""
        # Validated rather than constructed, so `number` is coerced exactly as pydantic would.
        return cls.model_validate(
            {
                "workspace_id": str(raw.get("workspace_id", "")),
                "label": str(raw.get("label") or ""),
                "number": raw.get("number"),
                "status": AgentStatus.coerce(raw.get("agent_status")),
                "focused": bool(raw.get("focused", False)),
                "pane_count": _to_int(raw.get("pane_count") or 0),
                "tab_count": _to_int(raw.get("tab_count") or 0),
            }
        )


def _shorter_title(raw_title: object, stripped: object) -> str:
    """Pick between herdr's two TERMINAL title fields: the SHORTER one trimmed."""
    candidates = [
        text for text in (str(stripped or "").strip(), str(raw_title or "").strip()) if text
    ]
    if not candidates:
        return ""
    return min(candidates, key=len)


def _chosen_name(raw: _Raw, pane: _Raw | None) -> str:
    """What to call this pane: the most deliberate name anyone gave it."""
    for text in (
        (pane or {}).get("label"),
        raw.get("title") or (pane or {}).get("title"),
        raw.get("name"),
    ):
        chosen = str(text or "").strip()
        if chosen:
            return chosen
    return _shorter_title(raw.get("terminal_title"), raw.get("terminal_title_stripped"))


def _coerce_session(raw: object) -> tuple[str, str]:
    """Normalize the ``agent_session`` field into ``(session_id, provider_kind)``."""
    if isinstance(raw, str):
        return raw.strip(), ""
    if isinstance(raw, dict):
        session = cast(_Raw, raw)
        return str(session.get("value") or "").strip(), str(session.get("agent") or "")
    return "", ""


class Agent(BaseModel):
    """One agent, normalized and joined to its workspace label."""

    model_config = ConfigDict(frozen=True)

    session: SessionId
    target: PaneId
    title: str = ""
    status: AgentStatus = AgentStatus.UNKNOWN
    agent: str = ""
    cwd: str = ""
    foreground_cwd: str = ""
    workspace: str = ""
    workspace_id: WorkspaceId = WorkspaceId("")
    workspace_number: int | None = None
    # Front pane of its tab, so several agents carry it; the global one is Snapshot.focused_pane_id.
    focused: bool = False
    revision: int = 0
    state_change_seq: int = 0

    @computed_field
    @property
    def rank(self) -> int:
        """Urgency rank; lower is more urgent. Templates sort and style on this."""
        return rank(self.status)

    def feed_key(self, urgency: int | None = None) -> tuple[int, int, int, str]:
        """The feed's whole order; ``urgency`` replaces the rank and nothing else."""
        return (
            self.rank if urgency is None else urgency,
            -self.state_change_seq,
            self.workspace_number or 0,
            self.target,
        )

    @property
    def sort_key(self) -> tuple[int, int, int, str]:
        """Deterministic feed order: urgency, recency, then workspace, then pane."""
        return self.feed_key()

    @property
    def seat(self) -> Seat:
        """This agent's pane, held to its own session."""
        return Seat(self.target, self.session)

    @property
    def project(self) -> str:
        """Basename of ``cwd`` — what a phone-width card can actually show."""
        return self.cwd.rstrip("/").rpartition("/")[2]

    @classmethod
    def from_raw(
        cls,
        raw: _Raw,
        workspaces: Mapping[WorkspaceId, Workspace] | None = None,
        panes: Mapping[str, _Raw] | None = None,
    ) -> Self:
        """Normalize one snapshot ``agents[]`` entry, joining workspace and pane."""
        workspace_id = str(raw.get("workspace_id") or "")
        ws = (workspaces or {}).get(WorkspaceId(workspace_id))
        pane_id = str(raw.get("pane_id") or "")
        session_id, session_agent = _coerce_session(raw.get("agent_session"))
        return cls(
            session=SessionId(session_id),
            target=PaneId(pane_id),
            title=_chosen_name(raw, (panes or {}).get(pane_id)),
            status=AgentStatus.coerce(raw.get("agent_status")),
            agent=str(raw.get("agent") or session_agent),
            cwd=str(raw.get("cwd") or ""),
            foreground_cwd=str(raw.get("foreground_cwd") or raw.get("cwd") or ""),
            workspace=ws.label if ws else "",
            workspace_id=WorkspaceId(workspace_id),
            workspace_number=ws.number if ws else None,
            focused=bool(raw.get("focused", False)),
            revision=_to_int(raw.get("revision") or 0),
            state_change_seq=_to_int(raw.get("state_change_seq") or 0),
        )


class Presence(StrEnum):
    """What became of a session between when it was captured and now."""

    HERE = "here"
    MOVED = "moved"
    GONE = "gone"


class SessionResolution(BaseModel):
    """The answer to "is this still the agent I was looking at?"."""

    model_config = ConfigDict(frozen=True)

    session: SessionId
    presence: Presence
    expected_target: PaneId | None = None
    agent: Agent | None = None
    occupant: Agent | None = None

    @property
    def ok(self) -> bool:
        """True only when the session is alive *and* still at the expected pane."""
        return self.presence is Presence.HERE

    @property
    def target(self) -> PaneId | None:
        """The address to drive right now, or ``None`` when there is nothing to drive."""
        return self.agent.target if self.agent is not None else None

    @property
    def message(self) -> str:
        """One line a drill-in or a tapped notification can show instead of guessing."""
        if self.presence is Presence.HERE:
            return ""
        if self.presence is Presence.MOVED:
            return f"This agent moved from pane {self.expected_target} to {self.target}."
        if self.occupant is not None:
            return (
                f"This agent is gone. Pane {self.expected_target} now belongs to a different agent."
            )
        return "This agent is gone."


class Snapshot(BaseModel):
    """A whole session at one instant, agents already ranked most-urgent-first."""

    model_config = ConfigDict(frozen=True)

    protocol: int = 0
    version: str = ""
    expected_protocol: int = EXPECTED_PROTOCOL
    agents: tuple[Agent, ...] = ()
    workspaces: tuple[Workspace, ...] = ()
    focused_pane_id: str | None = None

    @classmethod
    def from_raw(cls, raw: _Raw, *, expected_protocol: int | None = None) -> Self:
        """Build from the inner ``snapshot`` object of a runner payload."""
        workspaces = tuple(Workspace.from_raw(w) for w in _records(raw.get("workspaces")))
        by_id = {w.workspace_id: w for w in workspaces}
        by_pane = {str(p.get("pane_id") or ""): p for p in _panes(raw.get("panes"))}
        agents = sorted(
            (Agent.from_raw(a, by_id, by_pane) for a in _records(raw.get("agents"))),
            key=lambda a: a.sort_key,
        )
        # Validated rather than constructed, so the focus ids are checked exactly as pydantic would.
        return cls.model_validate(
            {
                "protocol": _to_int(raw.get("protocol") or 0),
                "version": str(raw.get("version") or ""),
                "expected_protocol": (
                    EXPECTED_PROTOCOL if expected_protocol is None else expected_protocol
                ),
                "agents": tuple(agents),
                "workspaces": workspaces,
                "focused_pane_id": raw.get("focused_pane_id"),
            }
        )

    @property
    def blocked(self) -> tuple[Agent, ...]:
        """Agents that need a human. These are the ones a notification is for."""
        return tuple(a for a in self.agents if a.status is AgentStatus.BLOCKED)

    @property
    def counts(self) -> dict[AgentStatus, int]:
        """Per-status tally for the header ("N working · M need you")."""
        tally = Counter(a.status for a in self.agents)
        return {status: tally.get(status, 0) for status in AgentStatus}

    def by_session(self, session: SessionId) -> Agent | None:
        """Look an agent up by its stable identity."""
        text = str(session)
        if not text.strip():
            return None
        as_session_id(text)  # raises if an address was passed as an identity
        return next((a for a in self.agents if a.session == text), None)

    def by_target(self, target: PaneId) -> Agent | None:
        """Look an agent up by pane address. Verify ``session`` before driving it."""
        text = str(target)
        if not text.strip():
            return None
        as_pane_id(text)  # raises if an identity was passed as an address
        return next((a for a in self.agents if a.target == text), None)

    def resolve_session(
        self,
        session: SessionId,
        *,
        expected_target: PaneId | None = None,
    ) -> SessionResolution:
        """Reconcile a remembered identity against this snapshot."""
        agent = self.by_session(session)
        occupant = self.by_target(expected_target) if expected_target is not None else None
        if agent is None:
            presence = Presence.GONE
        elif expected_target is None or agent.target == expected_target:
            presence = Presence.HERE
        else:
            presence = Presence.MOVED
        return SessionResolution(
            session=session,
            presence=presence,
            expected_target=expected_target,
            agent=agent,
            occupant=occupant if occupant is not agent else None,
        )
