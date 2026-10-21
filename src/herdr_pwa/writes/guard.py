"""What may be typed, how a send is known to have landed, and when it never can be."""

import re
from typing import Final

from herdr_pwa import glyphs
from herdr_pwa.engine import BlockedPrompt, MenuShape
from herdr_pwa.panetext import pane_lines, plain_text
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


def asks_a_question(screen: str) -> bool:
    """Does this screen show a question that a typed prompt would answer instead?"""
    prompt = BlockedPrompt.parse(screen)
    # Either recognition stops a prompt: a hint footer, or any option run, checkbox screens included.
    return (
        prompt.answerable
        or prompt.is_choice
        or prompt.drew_rows
        or prompt.shape is not MenuShape.NONE
    )


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
