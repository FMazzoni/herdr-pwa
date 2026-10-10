"""Tapping a key: only one the freshly read screen offers, at the agent holding the session now."""

import logging
from dataclasses import dataclass
from typing import Final

from herdr_pwa.audit import KEYS, Write
from herdr_pwa.engine.client import INTERRUPT_KEY, HerdrClient
from herdr_pwa.engine.dialog import BlockedPrompt
from herdr_pwa.engine.model import Agent, AgentStatus, PaneId, SessionId
from herdr_pwa.engine.transport import EngineError
from herdr_pwa.writes.keys import PAD_KEYS, allowed_keys, key_ends_question
from herdr_pwa.writes.outcome import (
    UNREADABLE,
    Evidence,
    Outcome,
    Poll,
    Watched,
    audit_result,
    watch,
)
from herdr_pwa.writes.prompt import NO_SESSION
from herdr_pwa.writes.refusal import Refusal, Refused, refusal_for
from herdr_pwa.writes.resolve import Writer, agent_now, attempt, snapshot_now

log = logging.getLogger(__name__)

_VERB: Final = "sent"
_STARTING_KEYS: Final = frozenset({*PAD_KEYS, INTERRUPT_KEY})


@dataclass(frozen=True, slots=True)
class KeySent:
    """A key the runner took, what watching the pane established, and the screen it left."""

    target: PaneId
    key: str
    outcome: Outcome
    evidence: Evidence
    # Whether this key was aimed at ending the question rather than moving inside it.
    ended: bool
    # The question as read after the key; ``None`` when it is gone, so no stale controls are drawn.
    prompt: BlockedPrompt | None


def _question_now(client: HerdrClient, agent: Agent) -> BlockedPrompt | Refused | None:
    if agent.status is not AgentStatus.BLOCKED:
        return None
    try:
        return client.blocked_prompt(agent.target, session=agent.session)
    except EngineError as exc:
        log.warning("key refused: %s could not be read first: %s", agent.target, exc)
        return Refused(Refusal.UNREADABLE, _VERB, detail=str(exc))


def _check(
    key: str, prompt: BlockedPrompt | None, drawn_from: str, *, confirmed: bool
) -> Refused | None:
    if key not in allowed_keys(prompt):
        return Refused(Refusal.KEY_NOT_OFFERED, _VERB, prompt=prompt)
    # An interrupt has to work while the screen is moving, so it skips the fingerprint.
    if key == INTERRUPT_KEY:
        return None if confirmed else Refused(Refusal.CONFIRM, _VERB, prompt=prompt)
    if prompt is not None and drawn_from != prompt.fingerprint:
        return Refused(Refusal.SCREEN_CHANGED, _VERB, screen=prompt.fingerprint, prompt=prompt)
    return None


def _reader(client: HerdrClient, target: PaneId, session: SessionId) -> Poll:
    try:
        now = client.agent(target)
        if now.session != session or now.status is not AgentStatus.BLOCKED:
            return None
        prompt = client.blocked_prompt(target, session=session)
    except EngineError as exc:
        log.warning("could not watch the key land at %s: %s", target, exc)
        return UNREADABLE
    # Still blocked and nothing parsed is an unreadable screen, not an answered question.
    return prompt if prompt.identity else UNREADABLE


def _watched(writer: Writer, agent: Agent, prompt: BlockedPrompt | None, *, ended: bool) -> Watched:
    if prompt is None or not prompt.identity:
        return Watched(Outcome.ACCEPTED, Evidence.DELIVERED)
    return watch(
        lambda: _reader(writer.client, agent.target, agent.session),
        aimed=prompt.identity,
        ends=ended,
        pacing=writer.pacing,
    )


def _press(
    writer: Writer, agent: Agent, key: str, prompt: BlockedPrompt | None
) -> KeySent | Refused:
    entry = attempt(writer.audit, Write(KEYS, agent.target, agent.session, key), verb=_VERB)
    if isinstance(entry, Refused):
        return entry
    ended = key_ends_question(prompt, key)
    with entry:
        try:
            _ = writer.client.send_keys(agent.target, key, session=agent.session)
        except EngineError as exc:
            entry.outcome(f"refused:{type(exc).__name__}")
            return Refused(refusal_for(exc), _VERB, detail=str(exc), prompt=prompt)
        # Written first, so a watcher that raises still leaves a record of a key that went out.
        entry.outcome(audit_result(Outcome.ACCEPTED, Evidence.DELIVERED))
        seen = _watched(writer, agent, prompt, ended=ended)
        entry.outcome(audit_result(seen.outcome, seen.evidence))
    return KeySent(agent.target, key, seen.outcome, seen.evidence, ended, seen.prompt)


@dataclass(frozen=True, slots=True)
class KeyAsk:
    """One tapped control: who it is for, the key, and the screen it was drawn from."""

    session: SessionId
    key: str
    expected_target: PaneId | None = None
    # The fingerprint of the screen the control was drawn from.
    drawn_from: str = ""
    confirmed: bool = False


def send_key(writer: Writer, ask: KeyAsk) -> KeySent | Refused:
    """Send one key the screen offers right now to the agent holding the session."""
    agent = agent_now(writer.client, ask.session, expected_target=ask.expected_target, verb=_VERB)
    if isinstance(agent, Refused):
        return agent
    key = ask.key.strip().lower()
    prompt = _question_now(writer.client, agent)
    if isinstance(prompt, Refused):
        if key != INTERRUPT_KEY:
            return prompt
        prompt = None
    refused = _check(key, prompt, ask.drawn_from, confirmed=ask.confirmed)
    if refused is not None:
        return refused
    return _press(writer, agent, key, prompt)


def send_starting_key(
    writer: Writer, pane: PaneId, key: str, *, confirmed: bool = False
) -> KeySent | Refused:
    """Send a pad key to a session-less agent stopped during launch (a trust dialog), by address."""
    key = key.strip().lower()
    if key not in _STARTING_KEYS:
        return Refused(Refusal.KEY_NOT_OFFERED, _VERB)
    snap = snapshot_now(writer.client, verb=_VERB)
    if isinstance(snap, Refused):
        return snap
    agent = snap.by_target(pane)
    if agent is None or agent.status is not AgentStatus.BLOCKED:
        return Refused(Refusal.NOT_WAITING, _VERB)
    if agent.session:
        return Refused(Refusal.HAS_IDENTITY, _VERB)
    if key == INTERRUPT_KEY and not confirmed:
        return Refused(Refusal.CONFIRM, _VERB)
    # The wire check then asserts the pane still holds no session at all.
    return _press(writer, agent.model_copy(update={"session": NO_SESSION}), key, None)
