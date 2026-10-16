"""Reads the herd through the shared cache and hands it to the decision functions."""

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from herdr_pwa.decisions import Fetched, HerdDecision, decide_herd, effective_status
from herdr_pwa.engine import (
    Agent,
    AgentStatus,
    BlockedPrompt,
    EngineError,
    InvalidRequest,
    Snapshot,
    as_session_id,
)
from herdr_pwa.web.deps import WebState
from herdr_pwa.web.failures import AgentGone

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Herd:
    """One snapshot and every decision the feed renders from it."""

    snapshot: Snapshot
    decision: HerdDecision


def _prompt(web: WebState, agent: Agent) -> BlockedPrompt | None:
    # Keyed by revision, so concurrent polls share one detection read per screen.
    key = f"prompt:{agent.session}:{agent.revision}"
    try:
        return web.cache.read(
            key, lambda: web.client.blocked_prompt(agent.target, session=agent.session)
        ).value
    except EngineError as exc:
        log.info("no prompt read for %s: %s", agent.target, exc)
        return None


def unread_of(web: WebState, agents: Iterable[Agent]) -> Mapping[str, bool] | None:
    """Which sessions' latest work the phone has not shown, noting what moved; None untracked."""
    seen = web.seen
    if seen is None:
        return None
    # Noted on every read, so a finish is stamped when it is first drawn, not when push ticks.
    seen.note(agents)
    return seen.unread_map()


def agent_status(web: WebState, agent: Agent) -> AgentStatus:
    """The status one agent's page shows, by the same rule as its feed card."""
    return effective_status(agent, unread_of(web, (agent,)))


def mark_read(web: WebState, session: str) -> None:
    """The phone showed this agent: its current state is observed first, then stamped read."""
    seen = web.seen
    if seen is None:
        return
    try:
        agents = web.snapshot().value.agents
    except EngineError as exc:
        log.info("marking %s read without the herd: %s", session, exc)
        agents = ()
    # A session the ledger never saw would be seeded at seq 0 and turn unread at its next look.
    seen.observe(agents)
    seen.mark_seen(session)


def load_herd(web: WebState) -> Herd:
    """The shared snapshot, decided; raises only when no snapshot was ever read."""
    entry = web.snapshot()
    snapshot = entry.value
    blocked = (a for a in snapshot.agents if a.status is AgentStatus.BLOCKED and a.session)
    prompts: dict[str, BlockedPrompt] = {
        a.session: p for a in blocked if (p := _prompt(web, a)) is not None
    }
    now = web.clock()
    failure = type(entry.error).__name__ if entry.error is not None else None
    decision = decide_herd(
        snapshot.agents,
        prompts=prompts,
        unread=unread_of(web, snapshot.agents),
        fetched=Fetched(now, now - entry.age, failure),
    )
    return Herd(snapshot, decision)


def find_agent(web: WebState, session: str) -> Agent:
    """The agent holding ``session`` in the current snapshot, or a failure the table maps."""
    try:
        identity = as_session_id(session)
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc
    agent = web.snapshot().value.by_session(identity)
    if agent is None:
        raise AgentGone(session)
    return agent
