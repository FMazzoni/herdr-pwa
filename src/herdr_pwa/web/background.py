"""What an agent started that still runs: its shells and agents, less what a dead process left."""

from herdr_pwa.decisions import AgentRow
from herdr_pwa.engine import AgentStatus, SessionUsage, Source, has_byte_frontier, locate_log
from herdr_pwa.web.deps import WebState
from herdr_pwa.web.stats import SpawnedTime, spawned_times

# A card says its agent is still at work only where herdr says the turn is over.
_RESTING = frozenset({AgentStatus.IDLE, AgentStatus.DONE})


def background_times(
    web: WebState, pane: str, usage: SessionUsage, now: float
) -> tuple[SpawnedTime, ...]:
    """*usage*'s background work as the page lists it, bounded by when *pane*'s process started."""
    # The runner is asked only when something is still open, which is rare.
    unended = any(task.started and not task.done for task in usage.agents)
    since = web.starts.started(pane) if unended and web.starts is not None else None
    return spawned_times(usage, now, since=since)


def card_background(web: WebState, row: AgentRow) -> bool:
    """Whether *row*'s agent is idle or done with work it started still running."""
    if (
        row.effective_status not in _RESTING
        or not row.session
        or not has_byte_frontier(row.harness)
    ):
        return False
    roots = web.settings.transcript_roots or None
    found = locate_log(Source.of(row.session, agent_kind=row.harness, root=roots))
    times = background_times(web, str(row.target), web.usage.read(found), web.wall())
    return any(time.running for time in times)
