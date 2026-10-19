"""Reads the herd through the shared cache and hands it to the decision functions."""

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from herdr_pwa.decisions import (
    Fetched,
    HerdDecision,
    Overlay,
    agent_row,
    decide_herd,
    effective_status,
    shows_question,
)
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
from herdr_pwa.web.naming import card_question

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Herd:
    """One snapshot and every decision the feed renders from it."""

    snapshot: Snapshot
    decision: HerdDecision


def prompt_of(web: WebState, agent: Agent) -> BlockedPrompt | None:
    """What *agent*'s screen asks, from the read its feed card and its page share per screen."""
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


def _asking(web: WebState, agents: Iterable[Agent]) -> frozenset[str]:
    return web.stalls.asking(agents, lambda agent: prompt_of(web, agent))


def overlay_of(web: WebState, agents: Iterable[Agent]) -> Overlay:
    """What the app read beside herdr's status: what the phone showed, who stalled, who asks."""
    herd = tuple(agents)
    return Overlay(unread_of(web, herd), web.stalls.of(herd), _asking(web, herd))


def asks(web: WebState, agent: Agent) -> bool:
    """Whether *agent*'s screen holds the question it waits on, by herdr's word or the app's read."""
    return shows_question(agent, Overlay(asking=_asking(web, (agent,))))


def agent_status(web: WebState, agent: Agent) -> AgentStatus:
    """The status one agent's page shows, by the same rule as its feed card."""
    return effective_status(agent, overlay_of(web, (agent,)))


def asked_by(web: WebState, agent: Agent) -> str:
    """What *agent*'s feed card says it is asking, from the same cached read; ``""`` for none."""
    blocked = bool(agent.session) and asks(web, agent)
    prompt = prompt_of(web, agent) if blocked else None
    return card_question(agent_row(agent, overlay_of(web, (agent,)), prompt))


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
    overlay = overlay_of(web, snapshot.agents)
    blocked = (a for a in snapshot.agents if a.session and shows_question(a, overlay))
    prompts: dict[str, BlockedPrompt] = {
        a.session: p for a in blocked if (p := prompt_of(web, a)) is not None
    }
    now = web.clock()
    failure = type(entry.error).__name__ if entry.error is not None else None
    decision = decide_herd(
        snapshot.agents,
        prompts=prompts,
        overlay=overlay,
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
