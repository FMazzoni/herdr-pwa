"""Typed shapes the rest of herdr-pwa renders from."""

import hashlib
import re
from collections import Counter
from enum import StrEnum
from typing import Any, NewType, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field

from herdr_pwa.protocol import EXPECTED_PROTOCOL as _EXPECTED_PROTOCOL


def screen_digest(screen: str) -> str:
    """A change detector for one rendered terminal screen."""
    return hashlib.sha256(screen.encode("utf-8", "replace")).hexdigest()[:16]


EXPECTED_PROTOCOL = _EXPECTED_PROTOCOL


SessionId = NewType("SessionId", str)

PaneId = NewType("PaneId", str)

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

    workspace_id: str
    label: str = ""
    number: int | None = None
    status: AgentStatus = AgentStatus.UNKNOWN
    focused: bool = False
    pane_count: int = 0
    tab_count: int = 0

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> Self:
        return cls(
            workspace_id=str(raw.get("workspace_id", "")),
            label=str(raw.get("label") or ""),
            number=raw.get("number"),
            status=AgentStatus.coerce(raw.get("agent_status")),
            focused=bool(raw.get("focused", False)),
            pane_count=int(raw.get("pane_count") or 0),
            tab_count=int(raw.get("tab_count") or 0),
        )


def _shorter_title(raw_title: object, stripped: object) -> str:
    """Pick between herdr's two TERMINAL title fields: the SHORTER one trimmed."""
    candidates = [
        text for text in (str(stripped or "").strip(), str(raw_title or "").strip()) if text
    ]
    if not candidates:
        return ""
    return min(candidates, key=len)


def _chosen_name(raw: dict[str, Any], pane: dict[str, Any] | None) -> str:
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
        return str(raw.get("value") or "").strip(), str(raw.get("agent") or "")
    return "", ""


class Agent(BaseModel):
    """One agent, normalized and joined to its workspace label."""

    model_config = ConfigDict(frozen=True)

    session: SessionId = Field(
        description="Provider session id — the stable identity (see SessionId)."
    )
    target: PaneId = Field(description="Pane id: the address the runner is driven by (see PaneId).")
    title: str = Field(
        default="",
        description="What to call this pane — the most deliberate name anyone "
        "gave it (see _chosen_name), glyphs stripped.",
    )
    status: AgentStatus = AgentStatus.UNKNOWN
    agent: str = Field(default="", description="Provider kind, e.g. 'claude'.")
    cwd: str = ""
    foreground_cwd: str = ""
    workspace: str = Field(default="", description="Workspace label, joined by workspace_id.")
    workspace_id: str = ""
    workspace_number: int | None = None
    tab_id: str = ""
    focused: bool = Field(
        default=False,
        description=(
            "Front pane OF ITS TAB, per herdr — not the one globally focused "
            "pane, which is Snapshot.focused_pane_id. Several agents carry it."
        ),
    )
    revision: int = 0
    state_change_seq: int = 0

    @computed_field  # type: ignore
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
    def project(self) -> str:
        """Basename of ``cwd`` — what a phone-width card can actually show."""
        return self.cwd.rstrip("/").rpartition("/")[2]

    @classmethod
    def from_raw(
        cls,
        raw: dict[str, Any],
        workspaces: dict[str, Workspace] | None = None,
        panes: dict[str, dict[str, Any]] | None = None,
    ) -> Self:
        """Normalize one snapshot ``agents[]`` entry, joining workspace and pane."""
        workspace_id = str(raw.get("workspace_id") or "")
        ws = (workspaces or {}).get(workspace_id)
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
            workspace_id=workspace_id,
            workspace_number=ws.number if ws else None,
            tab_id=str(raw.get("tab_id") or ""),
            focused=bool(raw.get("focused", False)),
            revision=int(raw.get("revision") or 0),
            state_change_seq=int(raw.get("state_change_seq") or 0),
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
    expected_target: PaneId | None = Field(
        default=None, description="The address this session was last seen at, if known."
    )
    agent: Agent | None = Field(
        default=None, description="The agent this session resolves to now, if alive."
    )
    occupant: Agent | None = Field(
        default=None,
        description="Whoever holds ``expected_target`` now — a different agent "
        "whenever this is set and ``presence`` is not HERE.",
    )

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
    focused_tab_id: str | None = None
    focused_workspace_id: str | None = None

    @classmethod
    def from_raw(cls, raw: dict[str, Any], *, expected_protocol: int | None = None) -> Self:
        """Build from the inner ``snapshot`` object of a runner payload."""
        workspaces = tuple(Workspace.from_raw(w) for w in raw.get("workspaces") or ())
        by_id = {w.workspace_id: w for w in workspaces}
        by_pane = {
            str(p.get("pane_id") or ""): p for p in raw.get("panes") or () if isinstance(p, dict)
        }
        agents = sorted(
            (Agent.from_raw(a, by_id, by_pane) for a in raw.get("agents") or ()),
            key=lambda a: a.sort_key,
        )
        return cls(
            protocol=int(raw.get("protocol") or 0),
            version=str(raw.get("version") or ""),
            expected_protocol=(
                EXPECTED_PROTOCOL if expected_protocol is None else expected_protocol
            ),
            agents=tuple(agents),
            workspaces=workspaces,
            focused_pane_id=raw.get("focused_pane_id"),
            focused_tab_id=raw.get("focused_tab_id"),
            focused_workspace_id=raw.get("focused_workspace_id"),
        )

    @property
    def protocol_ok(self) -> bool:
        """False when the runner speaks a protocol this *view* was not built for."""
        return self.protocol == self.expected_protocol

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
