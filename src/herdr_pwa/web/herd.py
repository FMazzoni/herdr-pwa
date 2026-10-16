"""Reads the herd through the shared cache and hands it to the decision functions."""

import logging
from dataclasses import dataclass

from herdr_pwa.decisions import HerdDecision, decide_herd
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


def load_herd(web: WebState) -> Herd:
    """The shared snapshot, decided; raises only when no snapshot was ever read."""
    entry = web.snapshot()
    snapshot = entry.value
    blocked = (a for a in snapshot.agents if a.status is AgentStatus.BLOCKED and a.session)
    prompts: dict[str, BlockedPrompt] = {
        a.session: p for a in blocked if (p := _prompt(web, a)) is not None
    }
    now = web.clock()
    decision = decide_herd(
        snapshot.agents,
        prompts=prompts,
        unread=None,
        now=now,
        taken_at=now - entry.age,
        error=type(entry.error).__name__ if entry.error is not None else None,
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
