"""What may be typed, how a send is known to have landed, and when it never can be."""

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Final

from herdr_pwa import glyphs
from herdr_pwa.engine import AgentStatus, BlockedPrompt, MenuShape
from herdr_pwa.panetext import idle_composer, pane_lines, plain_text
from herdr_pwa.patterns import Rx

MAX_PROMPT_CHARS: Final = 4000

# Eight characters: enough that the needle is this send's, short enough to survive a wrap.
MIN_MATCH_CHARS: Final = 8


def _flat(text: str) -> str:
    return " ".join(text.split())


def _above_the_box(screen: str) -> str:
    # Text still in the agent's input box was typed, not submitted: it has not landed.
    return " ".join(plain_text(line) for line in pane_lines(screen, strip=False))


def landed(sent: str, before: str, after: str) -> bool:
    """True when the pane gained more copies of this send than it had before, outside its input."""
    needle = _flat(sent)[:MIN_MATCH_CHARS]
    if not needle:
        return False
    return _flat(_above_the_box(after)).count(needle) > _flat(_above_the_box(before)).count(needle)


NO_ECHO_MECHANISM: Final = (
    "This screen is asking for a password or passphrase, so the terminal has "
    "turned echo off: nothing appears as it is typed, on purpose. There is no "
    "way for this app to read the text back, now or later."
)

_COMPOSER_LEAD: Final = re.compile(rf"^\s*(?:{glyphs.one_of(glyphs.PROMPT)}|[$#%]\s)")

# Anchored at end of line with the colon required; a bare "Password:" reads as prose.
_NO_ECHO: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\[sudo\] password for \S+:\s*$", re.IGNORECASE),
    # ssh's ``user@host's password:``, entered at a word's start and its first @ after a letter.
    re.compile(r"(?<!\S)\S[^\s@]*+@\S+'s password:\s*$", re.IGNORECASE),
    re.compile(r"enter (?:the )?passphrase(?: for \S.*)?:\s*$", re.IGNORECASE),
    re.compile(r"(?:verify|confirm|retype|repeat) password:\s*$", re.IGNORECASE),
    re.compile(r"password \(again\):\s*$", re.IGNORECASE),
)


def no_echo_notice(screen: str) -> str:
    """Why nothing will ever appear on this screen, or ``""`` if that cannot be told."""
    tail = next((line.rstrip() for line in reversed(screen.splitlines()) if line.strip()), "")
    if not tail or _COMPOSER_LEAD.match(tail):
        return ""
    return NO_ECHO_MECHANISM if any(p.search(tail) for p in _NO_ECHO) else ""


def unsendable(text: str) -> bool | None:
    """``None`` when the text may be typed; ``True`` when empty; ``False`` when too long."""
    if not text.strip():
        return True
    if len(text) > MAX_PROMPT_CHARS:
        return False
    return None


class Drawn(Enum):
    """What a screen draws that a typed Return could answer, weakest first."""

    NOTHING = "nothing"
    # A numbered run and nothing else: a reply's list reads the same.
    ROWS = "rows"
    # Keys of its own, or a shape the parser recognised: the harness drew a dialog.
    DIALOG = "dialog"


def drawn_question(screen: str) -> Drawn:
    """What this screen draws that a typed prompt would answer instead."""
    prompt = BlockedPrompt.parse(screen)
    # A recognised dialog wins: hermes draws its dialogs above a live input box.
    if prompt.answerable or prompt.is_choice or prompt.shape is not MenuShape.NONE:
        return Drawn.DIALOG
    # Bare rows above an empty input box were said, not asked: a question takes the box's place.
    if not prompt.drew_rows or idle_composer(screen.splitlines()):
        return Drawn.NOTHING
    return Drawn.ROWS


def asks_a_question(screen: str) -> bool:
    """Does this screen show a question that a typed prompt would answer instead?"""
    # Either recognition stops a prompt: a hint footer, or any option run, checkbox screens included.
    return drawn_question(screen) is not Drawn.NOTHING


HERDR_ALONE: Final = "herdr says this agent is waiting on a question, though the screen shows none."
UNSETTLED: Final = (
    "herdr says this agent is {herdr}, but the screen shows a question, and nothing the agent "
    "recorded can settle which is right."
)
OVERRULED: Final = (
    "herdr says this agent is {herdr}, but the screen and the agent's own record both show a "
    "question waiting."
)
KEYED: Final = (
    "herdr says this agent is {herdr} and its own record shows nothing waiting, but the screen "
    "draws a dialog with keys of its own."
)
RECORDED: Final = (
    "herdr says this agent is {herdr} and the screen shows no question, but its own record holds "
    "a call still waiting on an answer."
)
HELD_BOTH: Final = (
    "herdr says this agent is waiting and its own record holds a call still waiting on an answer, "
    "though the screen shows no question."
)
INSISTED: Final = (
    "herdr said this agent was waiting on a question, but the screen showed none, so this was "
    "sent on your second tap."
)
SCREEN_ALONE: Final = (
    "The screen looked like a question, but herdr says this agent is {herdr} and its own record "
    "shows nothing waiting, so this was sent."
)


@dataclass(frozen=True, slots=True)
class QuestionCall:
    """Whether a typed prompt is refused for a question, and every signal the call was made on."""

    refuse: bool
    herdr: AgentStatus
    screen: Drawn
    # The harness's own record: a call still unanswered, none, or ``None`` when nothing says.
    record: bool | None = None
    # Which signal disagreed with the call, in words for the person; empty when none did.
    doubt: str = ""
    # A refusal a second deliberate send may override: only herdr's word stood against it.
    overridable: bool = False


def question_call(
    screen: str, herdr: AgentStatus, record: Callable[[], bool | None], *, insisted: bool = False
) -> QuestionCall:
    """Refuse a prompt where the signals there are agree a question waits; name any that did not.

    Args:
        insisted: The person sent it again after a refusal herdr's word alone made.
    """
    drawn = drawn_question(screen)
    if herdr is AgentStatus.UNKNOWN:
        # A shell, or a harness herdr has no word for: the screen is all there is.
        return QuestionCall(refuse=drawn is not Drawn.NOTHING, herdr=herdr, screen=drawn)
    if drawn is Drawn.NOTHING:
        return _unasked(herdr, record, insisted=insisted)
    if herdr is AgentStatus.BLOCKED:
        return QuestionCall(refuse=True, herdr=herdr, screen=drawn)
    # Read only here: the one call the harness's record decides.
    return _recorded(drawn, herdr, record())


def _unasked(
    herdr: AgentStatus, record: Callable[[], bool | None], *, insisted: bool
) -> QuestionCall:
    nothing = Drawn.NOTHING
    # A working agent's running call is no question, and its screen is not read for one.
    if herdr is AgentStatus.WORKING:
        return QuestionCall(refuse=False, herdr=herdr, screen=nothing)
    # herdr reads Claude's screen with the same box rule, so the record is the vote that differs.
    pending = record()
    if herdr is not AgentStatus.BLOCKED:
        doubt = RECORDED.format(herdr=herdr) if pending else ""
        return QuestionCall(
            refuse=bool(pending), herdr=herdr, screen=nothing, record=pending, doubt=doubt
        )
    if pending:
        return QuestionCall(
            refuse=True, herdr=herdr, screen=nothing, record=pending, doubt=HELD_BOTH
        )
    # A herdr stuck on its wait must not lock the agent out: a second deliberate send passes.
    return QuestionCall(
        refuse=not insisted,
        herdr=herdr,
        screen=nothing,
        record=pending,
        doubt=INSISTED if insisted else HERDR_ALONE,
        overridable=not insisted,
    )


def _recorded(drawn: Drawn, herdr: AgentStatus, pending: bool | None) -> QuestionCall:
    if pending is None:
        # herdr's stop is measured to miss a dialog (hermes's long approval), so the screen holds.
        wording, refuse = UNSETTLED, True
    elif pending:
        wording, refuse = OVERRULED, True
    elif drawn is Drawn.DIALOG:
        # A picker or trust prompt the harness draws itself is no tool call, so the record misses it.
        wording, refuse = KEYED, True
    else:
        wording, refuse = SCREEN_ALONE, False
    doubt = wording.format(herdr=herdr)
    return QuestionCall(refuse=refuse, herdr=herdr, screen=drawn, record=pending, doubt=doubt)


PATTERNS: Final = (
    Rx(
        _COMPOSER_LEAD,
        ("\u276f draft", "> x", "$ ls", "  # note"),
        ("$ls", "password:", "x > y"),
        "match",
    ),
    Rx(
        _NO_ECHO[0],
        ("[sudo] password for user: ", "[SUDO] Password for u:"),
        ("[sudo] password for user: typed", "[sudo] password for :"),
    ),
    Rx(
        _NO_ECHO[1],
        ("user@host's password:", "git@example.com's password: "),
        ("user@host's password: x", "'s password:"),
    ),
    Rx(
        _NO_ECHO[2],
        (
            "Enter passphrase:",
            "Enter passphrase for key '/home/user/.ssh/id_ed25519':",
            "enter the passphrase:",
        ),
        ("Enter passphrase", "enter passphrase for:"),
    ),
    Rx(_NO_ECHO[3], ("Verify password:", "Retype password: "), ("password:", "new password:")),
    Rx(_NO_ECHO[4], ("Password (again):",), ("Password again:",)),
)
