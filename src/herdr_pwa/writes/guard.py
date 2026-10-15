"""What must not be typed blind, how a send is known to have landed, and when it never can be."""

import re
from typing import Final

from herdr_pwa.engine.dialog import BlockedPrompt, MenuShape

MAX_PROMPT_CHARS: Final = 4000

# What may sit in front of a command and still leave it at the start of a line.
_LEAD: Final = re.compile(r"^(?:\s|[>*$`]|-(?=\s))*")

# What runs the command after it rather than being it: an alias-bypassing backslash, `command`,
# `env` with its flags and assignments, a bare assignment, and the wrappers that only re-launch.
_WRAPPER: Final = re.compile(
    r"^(?:\\(?=\w)"
    r"|(?:command|exec|nohup|time|nice|xargs)(?:\s+-\S+)*\s+"
    r"|env(?:\s+-\S+)*\s+"
    r"|[A-Za-z_]\w*=\S*\s+)"
)

# Where one command ends and the next one starts on the same line.
_SEPARATOR: Final = re.compile(r"[;&|]+")

_GIT: Final = r"^git(?:\s+-[Cc]\s+\S+)*\s+"

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
        re.compile(rf"{_GIT}push\b{_GAP}(?:--force(?:-with-lease)?|-f)\b"),
        "git push --force",
        "overwrites the branch on the remote, including commits nobody here has",
    ),
    (
        re.compile(rf"{_GIT}push\b{_GAP}(?:--delete|-d)\b"),
        "git push --delete",
        "deletes the branch on the remote for everyone",
    ),
    (
        re.compile(rf"{_GIT}reset\b{_GAP}--hard\b"),
        "git reset --hard",
        "throws away every uncommitted change, and git keeps no copy of them",
    ),
    (
        re.compile(rf"{_GIT}clean\b{_GAP}(?:-[a-zA-Z]*f[a-zA-Z]*|--force)\b"),
        "git clean -f",
        "deletes every untracked file, which by definition git has no copy of",
    ),
    (
        re.compile(rf"{_GIT}(?:checkout(?:\s+--)?|restore)\s+\.(?:\s|$)"),
        "git checkout or restore of everything",
        "discards every uncommitted change in the tree, and git keeps no copy of them",
    ),
    (
        re.compile(rf"{_GIT}branch\b{_GAP}(?:-D|--delete\s+--force|-df|-fd)\b"),
        "git branch -D",
        "deletes a branch even when its commits are merged nowhere else",
    ),
    (
        re.compile(rf"{_GIT}stash\s+clear\b"),
        "git stash clear",
        "drops every stash at once",
    ),
    (
        re.compile(rf"^find\b{_GAP}(?:-delete\b|-exec(?:dir)?\s+rm\b)"),
        "find with -delete",
        "deletes everything the search matches, and a search is easy to get wider than meant",
    ),
    (
        re.compile(rf"^rsync\b{_GAP}--delete"),
        "rsync --delete",
        "deletes whatever the destination has that the source does not",
    ),
    (
        re.compile(r"^shred\b"),
        "shred",
        "overwrites the file so that nothing can recover it",
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
        for command in _commands(_LEAD.sub("", raw, count=1)):
            for pattern, matched, why in _DESTRUCTIVE:
                if pattern.search(command):
                    return f"This prompt contains {matched}, which {why}."
    return ""


def _unwrapped(command: str) -> str:
    command = command.strip()
    while (bare := _WRAPPER.sub("", command, count=1)) != command:
        command = bare
    return command


def _commands(line: str) -> list[str]:
    # Prose takes a verb only at the start of a line; shell also starts one after a separator.
    return [_unwrapped(part) for part in _SEPARATOR.split(line)]


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
