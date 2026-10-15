"""Making, relabelling and closing workspaces, each against the herd as it is now."""

from dataclasses import dataclass
from typing import Final

from herdr_pwa.audit import CLOSE, Write
from herdr_pwa.engine.client import HerdrClient
from herdr_pwa.engine.model import Agent, AgentStatus, Snapshot, Workspace
from herdr_pwa.engine.transport import EngineError
from herdr_pwa.writes.refusal import Refusal, Refused, failed_result, refusal_for
from herdr_pwa.writes.resolve import Writer, attempt, workspace_now

MAX_WORKSPACE_LABEL: Final = 40


@dataclass(frozen=True, slots=True)
class WorkspaceCost:
    """What closing one workspace would end, counted off a snapshot taken now."""

    workspace: Workspace
    agents: tuple[Agent, ...]

    @property
    def total(self) -> int:
        """How many agents end with this workspace."""
        return len(self.agents)

    @property
    def working(self) -> int:
        """How many of them are mid-task."""
        return sum(a.status is AgentStatus.WORKING for a in self.agents)

    @property
    def blocked(self) -> int:
        """How many of them are waiting on a person."""
        return sum(a.status is AgentStatus.BLOCKED for a in self.agents)

    @property
    def reason(self) -> str:
        """Why closing it is worth a second deliberate tap, or ``""`` when it ends nothing."""
        if not self.total:
            return ""
        label = self.workspace.label or self.workspace.workspace_id
        noun = "agent" if self.total == 1 else "agents"
        return f"Closing {label} ends {self.total} {noun}, and nothing brings them back."

    def describe(self) -> str:
        """The close as one audit line: the label, the count, and every agent."""
        listed = ", ".join(f"{a.target} {a.status.value} {a.session!r}" for a in self.agents)
        return (
            f"label={self.workspace.label!r} agents={self.total} "
            f"working={self.working} blocked={self.blocked} [{listed}]"
        )


@dataclass(frozen=True, slots=True)
class WorkspaceDone:
    """A workspace write the runner took, and whether a snapshot afterwards agrees."""

    workspace_id: str
    verified: bool


def cost_of(workspace: Workspace, snapshot: Snapshot) -> WorkspaceCost:
    """What closing ``workspace`` ends, from ``snapshot``."""
    inside = tuple(a for a in snapshot.agents if a.workspace_id == workspace.workspace_id)
    return WorkspaceCost(workspace, inside)


def _bad_label(label: str, *, required: bool) -> bool:
    if required and not label:
        return True
    return len(label) > MAX_WORKSPACE_LABEL or any(ch < " " for ch in label)


def _after(client: HerdrClient, workspace_id: str) -> tuple[bool, Workspace | None]:
    # (read at all, what was found): an unreadable herd verifies nothing either way.
    try:
        snap = client.snapshot()
    except EngineError:
        return False, None
    return True, next((w for w in snap.workspaces if w.workspace_id == workspace_id), None)


def create_workspace(client: HerdrClient, label: str) -> WorkspaceDone | Refused:
    """Make an empty workspace; nothing is started in it, so nothing asks twice."""
    label = label.strip()
    if _bad_label(label, required=False):
        return Refused(Refusal.BAD_LABEL, "created")
    try:
        made = client.create_workspace(label=label)
    except EngineError as exc:
        return Refused(refusal_for(exc), "created", detail=str(exc))
    read, found = _after(client, made.workspace_id)
    return WorkspaceDone(made.workspace_id, read and found is not None)


def rename_workspace(client: HerdrClient, workspace_id: str, label: str) -> WorkspaceDone | Refused:
    """Relabel a workspace that exists now; reversible, so it asks nothing twice."""
    found = workspace_now(client, workspace_id, verb="renamed")
    if isinstance(found, Refused):
        return found
    label = label.strip()
    if _bad_label(label, required=True):
        return Refused(Refusal.BAD_LABEL, "renamed")
    try:
        _ = client.rename_workspace(found[0].workspace_id, label)
    except EngineError as exc:
        return Refused(refusal_for(exc), "renamed", detail=str(exc))
    read, now = _after(client, found[0].workspace_id)
    return WorkspaceDone(found[0].workspace_id, read and now is not None and now.label == label)


def _close_refusal(
    cost: WorkspaceCost, *, confirmed: bool, armed: bool, saw: int | None
) -> Refused | None:
    if not confirmed:
        return Refused(Refusal.CONFIRM, "closed", reason=cost.reason)
    if not cost.reason:
        return None
    if not armed:
        return Refused(Refusal.CONFIRM, "closed", reason=cost.reason, screen=str(cost.total))
    # The second tap carries the count it was shown; a different count now is a different close.
    if saw != cost.total:
        return Refused(Refusal.COUNT_CHANGED, "closed", reason=cost.reason, screen=str(cost.total))
    return None


@dataclass(frozen=True, slots=True)
class WorkspaceCloseAsk:
    """A workspace close: the first confirm, the armed second, and the count it was shown."""

    workspace_id: str
    confirmed: bool = False
    armed: bool = False
    saw: int | None = None


def close_workspace(writer: Writer, ask: WorkspaceCloseAsk) -> WorkspaceDone | Refused:
    """End every agent in a workspace, counted now, after the confirms its cost earns."""
    found = workspace_now(writer.client, ask.workspace_id, verb="closed")
    if isinstance(found, Refused):
        return found
    cost = cost_of(*found)
    refused = _close_refusal(cost, confirmed=ask.confirmed, armed=ask.armed, saw=ask.saw)
    if refused is not None:
        return refused
    wid = cost.workspace.workspace_id
    entry = attempt(writer.audit, Write(CLOSE, wid, payload=cost.describe()), verb="closed")
    if isinstance(entry, Refused):
        return entry
    with entry:
        try:
            _ = writer.client.close_workspace(wid)
        except EngineError as exc:
            entry.outcome(failed_result(exc))
            return Refused(refusal_for(exc), "closed", detail=str(exc))
        entry.outcome(f"closed:{cost.total}")
    read, now = _after(writer.client, wid)
    return WorkspaceDone(wid, read and now is None)
