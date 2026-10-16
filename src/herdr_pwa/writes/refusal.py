"""Why a write did not happen, and the one table the web layer maps it through."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, NamedTuple

from herdr_pwa.engine import (
    NOT_READY_CODE,
    PANE_BUSY_CODE,
    SESSION_MOVED_CODE,
    BlockedPrompt,
    EngineError,
    EngineMisconfigured,
    EngineTimeout,
    EngineUnavailable,
    InvalidRequest,
    ProtocolMismatch,
    RequestTooLarge,
    TargetNotFound,
)


class Refusal(StrEnum):
    """Every reason a write was not made, or was held for a second tap."""

    UNREACHABLE = "unreachable"
    GONE = "gone"
    EMPTY = "empty"
    TOO_LONG = "too_long"
    BAD_NAME = "bad_name"
    BAD_KIND = "bad_kind"
    BAD_MODEL = "bad_model"
    BAD_CWD = "bad_cwd"
    BAD_LABEL = "bad_label"
    NO_WORKSPACE = "no_workspace"
    UNREADABLE = "unreadable"
    QUESTION_SHOWING = "question_showing"
    SCREEN_CHANGED = "screen_changed"
    KEY_NOT_OFFERED = "key_not_offered"
    CONFIRM = "confirm"
    RESEND = "resend"
    ACKNOWLEDGE = "acknowledge"
    COUNT_CHANGED = "count_changed"
    IN_FLIGHT = "in_flight"
    NOT_READY = "not_ready"
    NOT_WAITING = "not_waiting"
    HAS_IDENTITY = "has_identity"
    SESSION_MOVED = "session_moved"
    PANE_BUSY = "pane_busy"
    PROTOCOL_DRIFT = "protocol_drift"
    RUNNER_REFUSED = "runner_refused"
    TIMED_OUT = "timed_out"
    ANSWER_LOST = "answer_lost"
    UNRECORDABLE = "unrecordable"
    MISCONFIGURED = "misconfigured"


class Failure(NamedTuple):
    """The HTTP status a refusal answers with, and the sentence it says."""

    status: int
    says: str


# The one failure table; `{verb}` is the write's past participle ("sent", "closed").
FAILURES: Final[dict[Refusal, Failure]] = {
    Refusal.UNREACHABLE: Failure(
        503, "Nothing was {verb}: the herd could not be asked where this is right now."
    ),
    Refusal.GONE: Failure(404, "Nothing was {verb}: this agent is no longer in the herd."),
    Refusal.EMPTY: Failure(400, "Nothing was {verb}: it is empty."),
    Refusal.TOO_LONG: Failure(400, "Nothing was {verb}: it is longer than this app sends at once."),
    Refusal.BAD_NAME: Failure(
        400,
        "Nothing was {verb}: a name starts with a letter and is letters, digits, - or _.",
    ),
    Refusal.BAD_KIND: Failure(
        400, "Nothing was {verb}: that kind is not one this machine can start."
    ),
    Refusal.BAD_MODEL: Failure(
        400, "Nothing was {verb}: that model is not one offered for this kind here."
    ),
    Refusal.BAD_CWD: Failure(
        400, "Nothing was {verb}: give the full path of a directory on this machine."
    ),
    Refusal.BAD_LABEL: Failure(
        400, "Nothing was {verb}: a workspace name is one line of at most 40 characters."
    ),
    Refusal.NO_WORKSPACE: Failure(404, "Nothing was {verb}: that workspace is not in the herd."),
    Refusal.UNREADABLE: Failure(
        503,
        "Nothing was {verb}: the pane could not be read, so there is no telling "
        "whether a question would swallow it.",
    ),
    Refusal.QUESTION_SHOWING: Failure(
        409,
        "Nothing was {verb}: this agent is showing a question, and the Return would "
        "answer it. Use the keys, or interrupt it.",
    ),
    Refusal.SCREEN_CHANGED: Failure(
        409, "Nothing was {verb}: the screen changed since this was drawn. Look again."
    ),
    Refusal.KEY_NOT_OFFERED: Failure(
        400, "Nothing was {verb}: that key is not one this screen offers."
    ),
    # Held for a second deliberate tap, not failed; legacy answered these 200 with the confirm.
    Refusal.CONFIRM: Failure(200, "Nothing was {verb} yet: confirm it."),
    # The same text again after an attempt that may have landed: held for "Send again anyway".
    Refusal.RESEND: Failure(
        200,
        "Nothing was {verb} yet: the last send of this may already have landed. Look at the "
        "Pane, and send it again only if it did not.",
    ),
    Refusal.ACKNOWLEDGE: Failure(
        200, "Nothing was {verb} yet: this agent is working. Acknowledge that too."
    ),
    Refusal.COUNT_CHANGED: Failure(
        409,
        "Nothing was {verb}: its agents, or what they are doing, changed since you "
        "confirmed. Look again.",
    ),
    # Another tab or phone is mid-write to the same agent, pane or workspace; its outcome decides.
    Refusal.IN_FLIGHT: Failure(
        409,
        "Nothing was {verb}: another write to the same agent or workspace is still in flight. "
        "Look at what it did before trying again.",
    ),
    Refusal.NOT_READY: Failure(
        409, "Nothing was {verb}: this agent is not accepting input right now."
    ),
    Refusal.NOT_WAITING: Failure(
        409, "Nothing was {verb}: the herd no longer says anything is waiting there."
    ),
    Refusal.HAS_IDENTITY: Failure(
        409, "Nothing was {verb}: this agent has its own page now. Open it there."
    ),
    Refusal.SESSION_MOVED: Failure(
        409, "Nothing was {verb}: that pane now belongs to a different agent."
    ),
    Refusal.PANE_BUSY: Failure(
        409, "Nothing was {verb}: the new pane never settled to a shell prompt."
    ),
    Refusal.PROTOCOL_DRIFT: Failure(
        502,
        "Nothing was {verb}: the runner is speaking a version this build cannot safely drive.",
    ),
    Refusal.RUNNER_REFUSED: Failure(502, "Nothing was {verb}: the runner refused it."),
    Refusal.TIMED_OUT: Failure(
        504,
        "The runner did not answer in time. It may or may not have been {verb}; "
        "look before trying again.",
    ),
    # The request went out whole and its answer did not come back, so it may have run.
    Refusal.ANSWER_LOST: Failure(
        502,
        "The runner took this and its answer was lost. It may or may not have been {verb}; "
        "look before trying again.",
    ),
    Refusal.UNRECORDABLE: Failure(
        503, "Nothing was {verb}: it could not be recorded in the audit log first."
    ),
    Refusal.MISCONFIGURED: Failure(500, "Nothing was {verb}: this box is misconfigured."),
}

# Checked in order, so a subclass is named before the class it extends.
_BY_CLASS: Final[tuple[tuple[type[EngineError], Refusal], ...]] = (
    (EngineMisconfigured, Refusal.MISCONFIGURED),
    (EngineTimeout, Refusal.TIMED_OUT),
    (EngineUnavailable, Refusal.UNREACHABLE),
    (RequestTooLarge, Refusal.TOO_LONG),
    (ProtocolMismatch, Refusal.PROTOCOL_DRIFT),
    (InvalidRequest, Refusal.RUNNER_REFUSED),
)

_BY_CODE: Final[dict[str, Refusal]] = {
    SESSION_MOVED_CODE: Refusal.SESSION_MOVED,
    NOT_READY_CODE: Refusal.NOT_READY,
    PANE_BUSY_CODE: Refusal.PANE_BUSY,
    "invalid_agent_name": Refusal.BAD_NAME,
}


def refusal_for(exc: EngineError) -> Refusal:
    """The refusal an engine failure during a write amounts to."""
    if exc.after_send:
        return Refusal.TIMED_OUT if isinstance(exc, EngineTimeout) else Refusal.ANSWER_LOST
    by_code = _BY_CODE.get(exc.code)
    if by_code is not None:
        return by_code
    for kind, refusal in _BY_CLASS:
        if isinstance(exc, kind):
            return refusal
    return Refusal.GONE if isinstance(exc, TargetNotFound) else Refusal.RUNNER_REFUSED


def failed_result(exc: EngineError) -> str:
    """The audit outcome of a write the runner call raised on: refused, or unknown once sent."""
    return f"{'unknown' if exc.after_send else 'refused'}:{type(exc).__name__}"


@dataclass(frozen=True, slots=True)
class Refused:
    """A write that was not made, with what the screen needs to say so."""

    refusal: Refusal
    verb: str = "sent"
    # Why, for the operator log; never shown to the caller.
    detail: str = ""
    # A confirm's own reason (the destructive verb, the workspace's cost).
    reason: str = ""
    # The screen digest a retry or a confirm must carry back.
    screen: str = ""
    # The question as read inside this request, so controls are redrawn against it.
    prompt: BlockedPrompt | None = None

    @property
    def status(self) -> int:
        """The HTTP status the failure table assigns."""
        return FAILURES[self.refusal].status

    @property
    def says(self) -> str:
        """The sentence the failure table assigns, in this write's own verb."""
        return FAILURES[self.refusal].says.format(verb=self.verb)

    @property
    def unsure(self) -> bool:
        """True when this write may have run after all, so it must not be offered again blind."""
        return self.refusal in (Refusal.TIMED_OUT, Refusal.ANSWER_LOST)

    @property
    def asks_again(self) -> bool:
        """True when the next send of this text has to say, in so many words, that it is again."""
        return self.unsure or self.refusal is Refusal.RESEND

    @property
    def held(self) -> bool:
        """True when nothing failed and a second tap would send it."""
        return self.refusal in (Refusal.CONFIRM, Refusal.ACKNOWLEDGE)
