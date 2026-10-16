"""Typing a prompt at an agent: look first, refuse what would be swallowed, read back, never resend."""

import logging
import threading
from dataclasses import dataclass, field
from typing import Final

from herdr_pwa.audit import OMITTED_NO_ECHO, PROMPT, Write
from herdr_pwa.engine import AgentStatus, EngineError, HerdrClient, PaneId, SessionId, screen_digest
from herdr_pwa.writes.guard import (
    asks_a_question,
    destructive_reason,
    landed,
    no_echo_notice,
    unsendable,
)
from herdr_pwa.writes.outcome import Evidence, Outcome, Pacing, audit_result
from herdr_pwa.writes.refusal import Refusal, Refused, failed_result, refusal_for
from herdr_pwa.writes.resolve import Writer, agent_now, attempt, one_at_a_time, snapshot_now

log = logging.getLogger(__name__)

_VERB: Final = "sent"
CONFIRM_ATTEMPTS: Final = 4
CONFIRM_INTERVAL: Final = 0.25
# Asserted on the wire for a by-address prompt: the pane must still hold no session.
NO_SESSION: Final = SessionId("")
READY_WAIT: Final = 8.0
READY_POLL: Final = 0.5
# Three identical reads in a row mean the harness has finished drawing itself.
SETTLED_LOOKS: Final = 3


@dataclass
class Unsure:
    """The text each session was last sent with an unknown outcome, until something is sent."""

    _guard: threading.Lock = field(default_factory=threading.Lock)
    _text: dict[SessionId, str] = field(default_factory=dict[SessionId, str])

    def note(self, session: SessionId, text: str) -> None:
        """Remember that ``text`` may or may not have reached ``session``."""
        with self._guard:
            self._text[session] = text.strip()

    def pending(self, session: SessionId, text: str) -> bool:
        """Is ``text`` the one whose last send to ``session`` may already have landed?"""
        with self._guard:
            return bool(session) and self._text.get(session) == text.strip()

    def clear(self, session: SessionId) -> None:
        """A send to ``session`` was taken, so what came before it is settled by the pane."""
        with self._guard:
            _ = self._text.pop(session, None)


# Per process, like the in-flight set; a restart forgets it, and the composer still says so.
UNSURE: Final = Unsure()


@dataclass(frozen=True, slots=True)
class PromptSent:
    """A prompt the runner took, and what reading the pane back established."""

    target: PaneId
    outcome: Outcome
    evidence: Evidence
    no_echo: str = ""

    @property
    def verified(self) -> bool:
        """True only when the pane gained this text; otherwise it is "sent, unverified"."""
        return self.outcome is Outcome.CONFIRMED


@dataclass(frozen=True, slots=True)
class _Send:
    target: PaneId
    session: SessionId
    text: str
    drawn_against: str
    confirmed: bool
    # A working agent's screen moves, so no digest is issued for it to carry back.
    issue: bool
    # Sent anyway after an attempt whose outcome is unknown; said in so many words.
    again: bool = False


def _text_refusal(text: str) -> Refused | None:
    empty = unsendable(text)
    if empty is None:
        return None
    return Refused(Refusal.EMPTY if empty else Refusal.TOO_LONG, _VERB)


def _read_now(client: HerdrClient, target: PaneId) -> str | Refused:
    try:
        return client.read(target, source="visible")
    except EngineError as exc:
        log.warning("prompt refused: %s could not be read first: %s", target, exc)
        return Refused(Refusal.UNREADABLE, _VERB, detail=str(exc))


def _preflight(send: _Send, screen: str) -> Refused | None:
    # A question is no screen to type against, and answering it changes it: it issues no digest.
    if asks_a_question(screen):
        return Refused(Refusal.QUESTION_SHOWING, _VERB)
    digest = screen_digest(screen) if send.issue else ""
    if send.drawn_against and send.drawn_against != screen_digest(screen):
        return Refused(Refusal.SCREEN_CHANGED, _VERB, screen=digest)
    # Before the destructive confirm, so its own confirm cannot stand in for this one.
    if not send.again and UNSURE.pending(send.session, send.text):
        return Refused(Refusal.RESEND, _VERB, screen=digest)
    reason = "" if send.confirmed else destructive_reason(send.text)
    if reason:
        return Refused(Refusal.CONFIRM, _VERB, reason=reason, screen=digest)
    return None


def _landed(client: HerdrClient, send: _Send, before: str, pacing: Pacing) -> bool:
    for _ in range(CONFIRM_ATTEMPTS):
        try:
            after = client.read(send.target, source="visible")
        except EngineError:
            after = ""
        if landed(send.text, before, after):
            return True
        pacing.sleep(CONFIRM_INTERVAL)
    return False


def _type(writer: Writer, send: _Send) -> PromptSent | Refused:
    client = writer.client
    before = _read_now(client, send.target)
    if isinstance(before, Refused):
        return before
    stop = _preflight(send, before)
    if stop is not None:
        return stop
    # Changes what is stored and said, never what is sent.
    no_echo = no_echo_notice(before)
    write = Write(PROMPT, send.target, send.session, send.text, OMITTED_NO_ECHO if no_echo else "")
    entry = attempt(writer.audit, write, verb=_VERB)
    if isinstance(entry, Refused):
        return entry
    with entry:
        try:
            _ = client.prompt(send.target, send.text, session=send.session)
        except EngineError as exc:
            entry.outcome(failed_result(exc))
            return _failed(client, send, exc)
        ok = _landed(client, send, before, writer.pacing)
        outcome, evidence = (
            (Outcome.CONFIRMED, Evidence.ECHO) if ok else (Outcome.ACCEPTED, Evidence.NOECHO)
        )
        entry.outcome(audit_result(outcome, evidence))
    UNSURE.clear(send.session)
    return PromptSent(send.target, outcome, evidence, no_echo)


def _failed(client: HerdrClient, send: _Send, exc: EngineError) -> Refused:
    refused = Refused(refusal_for(exc), _VERB, detail=str(exc))
    if not refused.unsure:
        return refused
    UNSURE.note(send.session, send.text)
    # The text comes back with a digest of the screen now, so the next tap is checked against it.
    after = _read_now(client, send.target)
    fresh = screen_digest(after) if send.issue and isinstance(after, str) else ""
    return Refused(refused.refusal, _VERB, detail=refused.detail, screen=fresh)


@dataclass(frozen=True, slots=True)
class PromptAsk:
    """A composer's prompt: who it is for, the text, and the screen it was typed against."""

    session: SessionId
    text: str
    expected_target: PaneId | None = None
    # The digest the composer was drawn against; empty claims nothing about the screen.
    drawn_against: str = ""
    confirmed: bool = False
    again: bool = False


def send_prompt(writer: Writer, ask: PromptAsk) -> PromptSent | Refused:
    """Type one prompt at the agent holding the session now, or say why not."""
    return one_at_a_time(f"session:{ask.session}", _VERB, lambda: _send_prompt(writer, ask))


def _send_prompt(writer: Writer, ask: PromptAsk) -> PromptSent | Refused:
    agent = agent_now(writer.client, ask.session, expected_target=ask.expected_target, verb=_VERB)
    if isinstance(agent, Refused):
        return agent
    refused = _text_refusal(ask.text)
    if refused is not None:
        return refused
    send = _Send(
        agent.target,
        ask.session,
        ask.text,
        ask.drawn_against,
        ask.confirmed,
        issue=agent.status is not AgentStatus.WORKING,
        again=ask.again,
    )
    return _type(writer, send)


def _look(client: HerdrClient, pane: PaneId, *, by_address: bool) -> str | Refused:
    # One look at a starting pane: its screen once it reads input, "" before that.
    try:
        started = client.started_agent(pane)
        screen = client.read(pane, source="visible") if started.ready else ""
    except EngineError as exc:
        return Refused(refusal_for(exc), _VERB, detail=str(exc))
    return Refused(Refusal.HAS_IDENTITY, _VERB) if by_address and started.session else screen


def _settled(
    client: HerdrClient, pane: PaneId, pacing: Pacing, *, by_address: bool = True
) -> Refused | None:
    deadline = pacing.clock() + READY_WAIT
    seen, still = "", 0
    while pacing.clock() < deadline:
        screen = _look(client, pane, by_address=by_address)
        if isinstance(screen, Refused):
            return screen
        still = still + 1 if screen and screen == seen else int(bool(screen))
        seen = screen
        if still >= SETTLED_LOOKS:
            return None
        pacing.sleep(READY_POLL)
    return Refused(Refusal.NOT_READY, _VERB)


def send_first_prompt(
    writer: Writer, pane: PaneId, text: str, *, drawn_against: str = "", confirmed: bool = False
) -> PromptSent | Refused:
    """Type the prompt that gives a late-identity agent its session, by address."""
    return one_at_a_time(
        f"pane:{pane}",
        _VERB,
        lambda: _send_first_prompt(
            writer, pane, text, drawn_against=drawn_against, confirmed=confirmed
        ),
    )


def _send_first_prompt(
    writer: Writer, pane: PaneId, text: str, *, drawn_against: str, confirmed: bool
) -> PromptSent | Refused:
    snap = snapshot_now(writer.client, verb=_VERB)
    if isinstance(snap, Refused):
        return snap
    agent = snap.by_target(pane)
    if agent is None:
        return Refused(Refusal.GONE, _VERB)
    # Once a session exists every write is keyed on it, never on the recyclable address.
    if agent.session:
        return Refused(Refusal.HAS_IDENTITY, _VERB)
    refused = _text_refusal(text) or _settled(writer.client, pane, writer.pacing)
    if refused is not None:
        return refused
    # A confirm counts only with the digest of the screen it was asked about.
    bound = confirmed and bool(drawn_against)
    return _type(writer, _Send(pane, NO_SESSION, text, drawn_against, bound, issue=True))


def send_opening_prompt(writer: Writer, pane: PaneId, text: str) -> PromptSent | Refused:
    """Type the new-agent form's prompt at the agent this request started, once it listens."""
    # The form held a destructive prompt for its confirm before anything was created.
    refused = _text_refusal(text) or _settled(writer.client, pane, writer.pacing, by_address=False)
    if refused is not None:
        return refused
    snap = snapshot_now(writer.client, verb=_VERB)
    if isinstance(snap, Refused):
        return snap
    agent = snap.by_target(pane)
    if agent is None:
        return Refused(Refusal.GONE, _VERB)
    # Keyed on whatever identity it has by now; the wire checks that it still holds the pane.
    return _type(writer, _Send(pane, agent.session, text, "", confirmed=True, issue=True))
