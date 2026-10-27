"""Tapping a key: only one the freshly read screen offers, at the agent holding the session now."""

import logging
from dataclasses import dataclass
from typing import Final

from herdr_pwa.audit import KEYS, Write
from herdr_pwa.decisions import asks_now, reads_dialog
from herdr_pwa.engine import (
    INTERRUPT_KEY,
    Agent,
    AgentStatus,
    BlockedPrompt,
    EngineError,
    HerdrClient,
    PaneId,
    PromptSource,
    SessionId,
    prompt_source,
)
from herdr_pwa.writes.keys import (
    PAD_KEYS,
    allowed_keys,
    control_token,
    key_ends_question,
    normalise_key,
)
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
from herdr_pwa.writes.refusal import Refusal, Refused, failed_result, refusal_for
from herdr_pwa.writes.resolve import Writer, agent_now, attempt, one_at_a_time, snapshot_now

log = logging.getLogger(__name__)

_VERB: Final = "sent"
# Esc with no question showing stops a working agent's turn, so it is confirmed like Ctrl+C.
ESC_KEY: Final = "esc"
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


@dataclass(frozen=True, slots=True)
class KeyAsk:
    """One tapped control: who it is for, the key, and the screen it was drawn from."""

    session: SessionId
    key: str
    expected_target: PaneId | None = None
    # The token of the question the control was drawn for (keys.control_token).
    drawn_from: str = ""
    confirmed: bool = False


def _question_now(
    client: HerdrClient, agent: Agent, roots: tuple[str, ...]
) -> BlockedPrompt | Refused | None:
    if not reads_dialog(agent.status):
        return None
    try:
        prompt = client.blocked_prompt(prompt_source(agent, roots or None))
    except EngineError as exc:
        if agent.status is not AgentStatus.BLOCKED:
            # Read only for a dialog herdr may have missed; failing that, it is the stop it said.
            return None
        log.warning("key refused: %s could not be read first: %s", agent.target, exc)
        return Refused(Refusal.UNREADABLE, _VERB, detail=str(exc))
    return prompt if asks_now(agent.status, prompt) else None


def stops_turn(key: str, status: AgentStatus, *, asked: bool) -> bool:
    """Whether *key* stops a turn rather than answering a screen: Esc while it works, unasked."""
    return key == ESC_KEY and status is AgentStatus.WORKING and not asked


def _check(
    key: str, prompt: BlockedPrompt | None, ask: KeyAsk, status: AgentStatus
) -> Refused | None:
    if key not in allowed_keys(prompt):
        return Refused(Refusal.KEY_NOT_OFFERED, _VERB, prompt=prompt)
    # An interrupt has to work while the screen is moving, so it skips the token check.
    if key == INTERRUPT_KEY:
        return None if ask.confirmed else Refused(Refusal.CONFIRM, _VERB, prompt=prompt)
    # With no question showing now, a control drawn under one is stale: Esc there interrupts a turn.
    now = control_token(prompt) if prompt is not None else ""
    if ask.drawn_from != now:
        return Refused(Refusal.SCREEN_CHANGED, _VERB, screen=now, prompt=prompt)
    if stops_turn(key, status, asked=prompt is not None) and not ask.confirmed:
        return Refused(Refusal.CONFIRM, _VERB, prompt=prompt)
    return None


def _reader(client: HerdrClient, source: PromptSource) -> Poll:
    try:
        now = client.agent(source.target)
        if now.session != source.session or not reads_dialog(now.status):
            return None
        prompt = client.blocked_prompt(source)
    except EngineError as exc:
        log.warning("could not watch the key land at %s: %s", source.target, exc)
        return UNREADABLE
    if not asks_now(now.status, prompt):
        return None
    # Still blocked and nothing parsed is an unreadable screen, not an answered question.
    return prompt if prompt.identity else UNREADABLE


def _watched(writer: Writer, agent: Agent, prompt: BlockedPrompt | None, *, ended: bool) -> Watched:
    if prompt is None or not prompt.identity:
        return Watched(Outcome.ACCEPTED, Evidence.DELIVERED)
    return watch(
        lambda: _reader(writer.client, prompt_source(agent, writer.stores or None)),
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
            _ = writer.client.send_keys(agent.seat, key)
        except EngineError as exc:
            entry.outcome(failed_result(exc))
            return Refused(refusal_for(exc), _VERB, detail=str(exc), prompt=prompt)
        # Written first, so a watcher that raises still leaves a record of a key that went out.
        entry.outcome(audit_result(Outcome.ACCEPTED, Evidence.DELIVERED))
        seen = _watched(writer, agent, prompt, ended=ended)
        entry.outcome(audit_result(seen.outcome, seen.evidence))
    return KeySent(agent.target, key, seen.outcome, seen.evidence, ended=ended, prompt=seen.prompt)


def send_key(writer: Writer, ask: KeyAsk) -> KeySent | Refused:
    """Send one key the screen offers right now to the agent holding the session."""
    # From the read the key is checked against to the last look at what it did, alone.
    return one_at_a_time(
        writer.memory, f"session:{ask.session}", _VERB, lambda: _send_key(writer, ask)
    )


def _send_key(writer: Writer, ask: KeyAsk) -> KeySent | Refused:
    agent = agent_now(writer, ask.session, expected_target=ask.expected_target, verb=_VERB)
    if isinstance(agent, Refused):
        return agent
    key = normalise_key(ask.key)
    prompt = _question_now(writer.client, agent, writer.stores)
    if isinstance(prompt, Refused):
        if key != INTERRUPT_KEY:
            return prompt
        prompt = None
    refused = _check(key, prompt, ask, agent.status)
    if refused is not None:
        return refused
    return _press(writer, agent, key, prompt)


@dataclass(frozen=True, slots=True)
class StartingKeyAsk:
    """A pad key for a session-less agent stopped during launch: its pane and the key."""

    pane: PaneId
    key: str
    confirmed: bool = False


def send_starting_key(writer: Writer, ask: StartingKeyAsk) -> KeySent | Refused:
    """Send a pad key to a session-less agent stopped during launch (a trust dialog), by address."""
    return one_at_a_time(
        writer.memory, f"pane:{ask.pane}", _VERB, lambda: _send_starting_key(writer, ask)
    )


def _send_starting_key(writer: Writer, ask: StartingKeyAsk) -> KeySent | Refused:
    key = normalise_key(ask.key)
    if key not in _STARTING_KEYS:
        return Refused(Refusal.KEY_NOT_OFFERED, _VERB)
    snap = snapshot_now(writer.client, verb=_VERB)
    if isinstance(snap, Refused):
        return snap
    agent = snap.by_target(ask.pane)
    if agent is None or agent.status is not AgentStatus.BLOCKED:
        return Refused(Refusal.NOT_WAITING, _VERB)
    if agent.session:
        return Refused(Refusal.HAS_IDENTITY, _VERB)
    if key == INTERRUPT_KEY and not ask.confirmed:
        return Refused(Refusal.CONFIRM, _VERB)
    # The wire check then asserts the pane still holds no session at all.
    return _press(writer, agent.model_copy(update={"session": NO_SESSION}), key, None)
