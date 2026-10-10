"""What must not be typed blind, how a send is known to have landed, and when it never can be."""

import re
from typing import Final

from herdr_pwa.engine.dialog import BlockedPrompt, MenuShape

MAX_PROMPT_CHARS: Final = 4000

# What may sit in front of a command and still leave it at the start of a line.
_LEAD: Final = re.compile(r"^(?:\s|[>*$`]|-(?=\s))*")

_RECURSIVE: Final = r"-(?:[a-qs-zA-Z]*[rR][a-zA-Z]*|-recursive)\b"

# The gap inside a pattern never crosses a command separator.
_GAP: Final = r"[^;&|]*?\s"

# Most specific first, so `git push --force` is named as itself rather than as `--force`.
_DESTRUCTIVE: Final[tuple[tuple[re.Pattern[str], str, str], ...]] = (
    (
        re.compile(rf"^rm\b{_GAP}{_RECURSIVE}"),
        "rm with a recursive flag",
        "deletes a whole directory tree, and nothing puts it back",
    ),
    (
        re.compile(rf"^git\s+push\b{_GAP}(?:--force(?:-with-lease)?|-f)\b"),
        "git push --force",
        "overwrites the branch on the remote, including commits nobody here has",
    ),
    (
        re.compile(r"^mkfs(?:\.[a-z0-9]+)?\b"),
        "mkfs",
        "formats a filesystem, destroying everything already on it",
    ),
    (
        re.compile(rf"^dd\b{_GAP}*if="),
        "dd with if=",
        "writes raw blocks over whatever is at of=, with no confirmation of its own",
    ),
    (
        re.compile(rf"^chmod\b{_GAP}{_RECURSIVE}"),
        "chmod with a recursive flag",
        "rewrites permissions down a whole tree, and the old ones are not recorded",
    ),
    (
        re.compile(r"^sudo\b"),
        "sudo",
        "runs as root, where the usual protections do not apply",
    ),
    (
        re.compile(r">\s*/(?:etc|usr|bin|sbin|lib|lib64|boot|dev|var|opt)/\S+"),
        "a redirect onto a system path",
        "truncates that file before anything is written to it",
    ),
    (
        re.compile(r"(?<![\w-])--force\b(?:\s+[-/.~]\S*|\s+\S*[/=]\S*)*\s*$"),
        "--force",
        "is the flag that turns a refusal into an action, and the refusal was the check",
    ),
)


def destructive_reason(text: str) -> str:
    """Why this prompt is worth a second deliberate tap, or ``""``."""
    for raw in text.splitlines():
        line = _LEAD.sub("", raw, count=1)
        for pattern, matched, why in _DESTRUCTIVE:
            if pattern.search(line):
                return f"This prompt contains {matched}, which {why}."
    return ""


# Eight characters: enough that the needle is this send's, short enough to survive a wrap.
MIN_MATCH_CHARS: Final = 8

_SPACE: Final = re.compile(r"\s+")


def _flat(text: str) -> str:
    return _SPACE.sub(" ", text).strip()


def landed(sent: str, before: str, after: str) -> bool:
    """True when the pane gained more copies of this send than it had before."""
    needle = _flat(sent)[:MIN_MATCH_CHARS]
    if not needle:
        return False
    return _flat(after).count(needle) > _flat(before).count(needle)


NO_ECHO_MECHANISM: Final = (
    "This screen is asking for a password or passphrase, so the terminal has "
    "turned echo off: nothing appears as it is typed, on purpose. There is no "
    "way for this app to read the text back, now or later."
)

_COMPOSER_LEAD: Final = re.compile(r"^\s*(?:[\u276f>]|[$#%]\s)")

# Anchored at end of line with the colon required; a bare "Password:" reads as prose.
_NO_ECHO: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\[sudo\] password for \S+:\s*$", re.IGNORECASE),
    re.compile(r"\S+@\S+'s password:\s*$", re.IGNORECASE),
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
