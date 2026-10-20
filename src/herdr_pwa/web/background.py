"""What an agent started that still runs: its shells and agents, less what a dead process left."""

from collections.abc import Iterable

from herdr_pwa.decisions import Overlay, Pending, effective_status
from herdr_pwa.engine import (
    SPAWNED_SHELL,
    Agent,
    AgentStatus,
    SessionUsage,
    Source,
    has_byte_frontier,
    locate_log,
)
from herdr_pwa.web.deps import WebState
from herdr_pwa.web.stats import SpawnedTime, spawned_times


def background_times(
    web: WebState, pane: str, usage: SessionUsage, now: float
) -> tuple[SpawnedTime, ...]:
    """*usage*'s background work as the page lists it, bounded by when *pane*'s process started."""
    # The runner is asked only when something is still open, which is rare.
    unended = any(task.started and not task.done for task in usage.agents)
    since = web.starts.started(pane) if unended and web.starts is not None else None
    return spawned_times(usage, now, since=since)


def pending_of(times: tuple[SpawnedTime, ...]) -> Pending:
    """What of *times* is still to come back: agents that will report, and shells the last turn
    started; a server an older turn left runs on without anyone waiting on it."""
    running = [time for time in times if time.running]
    shells = sum(1 for time in running if time.kind == SPAWNED_SHELL and time.fresh)
    return Pending(agents=sum(1 for time in running if time.kind != SPAWNED_SHELL), shells=shells)


def _pending(web: WebState, agent: Agent) -> Pending:
    roots = web.settings.transcript_roots or None
    found = locate_log(Source.of(agent.session, agent_kind=agent.agent, root=roots))
    usage = web.usage.read(found)
    return pending_of(background_times(web, str(agent.target), usage, web.wall()))


def pending_by_session(
    web: WebState, agents: Iterable[Agent], overlay: Overlay
) -> dict[str, Pending]:
    """Each idle agent's work still to come back, by session; only a harness with a log to read."""
    idle = [
        agent
        for agent in agents
        if agent.session
        and has_byte_frontier(agent.agent)
        and effective_status(agent, overlay) is AgentStatus.IDLE
    ]
    # Each idle card reads its agent's tally, so the cache holds them all.
    web.usage.fit(len(idle))
    found = {str(agent.session): _pending(web, agent) for agent in idle}
    return {session: pending for session, pending in found.items() if pending}
