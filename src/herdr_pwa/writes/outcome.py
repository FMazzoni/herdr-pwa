"""What a write is known to have done: an ack means bytes taken, never that the TUI acted."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Final

from herdr_pwa.clock import MonoClock, Sleep, mono_now
from herdr_pwa.engine import BlockedPrompt


class Outcome(StrEnum):
    """What this app established about a write, in the audit log's words."""

    ACCEPTED = "accepted"
    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"
    PARTIALLY_APPLIED = "partially_applied"


class Evidence(StrEnum):
    """What was observed, kept apart from what is concluded from it."""

    GONE = "gone"
    MOVED = "moved"
    UNMOVED = "unmoved"
    UNREAD = "unread"
    FLICKER = "flicker"
    DELIVERED = "delivered"
    ECHO = "echo"
    NOECHO = "noecho"
    # Passed on the harness's command line at launch, and not yet seen on the pane.
    ARGV = "argv"


class Unreadable(Enum):
    """The one value a watcher's reader returns when the pane could not be read."""

    TOKEN = "unreadable"  # noqa: S105 - a sentinel, not a secret


UNREADABLE: Final = Unreadable.TOKEN

# What one look returns: the question, ``None`` once nothing is waiting, or unreadable.
type Poll = BlockedPrompt | Unreadable | None


@dataclass(frozen=True, slots=True)
class Pacing:
    """The clock and sleep every wait in this package runs on, injected so tests never sleep."""

    sleep: Sleep = time.sleep
    clock: MonoClock = mono_now


# Two not-waiting reads in a row before the question is believed gone.
GONE_POLLS: Final = 2
# Two reads of the same other question in a row before the key is believed to have moved it on.
# One interval, never the whole budget: the point of ending on a move is ending fast.
MOVED_POLLS: Final = 2
ANSWER_INTERVAL: Final = 0.35
ANSWER_BUDGET: Final = 5.0
# A navigation key is judged quickly, or every second tap is refused as stale.
MOVE_INTERVAL: Final = 0.12
MOVE_BUDGET: Final = 0.36


@dataclass(frozen=True, slots=True)
class Watched:
    """What the watcher saw, and the last question it managed to read."""

    outcome: Outcome
    evidence: Evidence
    prompt: BlockedPrompt | None = None
    polls: int = 0


def audit_result(outcome: Outcome, evidence: Evidence) -> str:
    """The token the audit log records as this write's outcome."""
    return f"{outcome.value}:{evidence.value}"


# Mutable: one watch's running tally, updated on every poll.
@dataclass(slots=True)
class _Run:
    saw: Evidence = Evidence.UNREAD
    poll: BlockedPrompt | None = None
    last: BlockedPrompt | None = None
    polls: int = 0
    gone: int = 0
    # Consecutive reads of one other question, and which one.
    moved: int = 0
    moved_to: str = ""
    flickered: bool = False
    graced: bool = False

    def take_grace(self) -> bool:
        # One uncorroborated gone or moved frame earns one more look, and only one.
        if self.graced or not (self.gone or self.moved):
            return False
        self.graced = True
        return True

    def note(self, saw: Evidence, poll: Poll) -> None:
        # A flicker is a transition, gone and then waiting again, not a value.
        self.flickered = self.flickered or (saw is not Evidence.GONE and self.gone > 0)
        self.saw = saw
        self.polls += 1
        self.gone = self.gone + 1 if saw is Evidence.GONE else 0
        self.poll = poll if isinstance(poll, BlockedPrompt) else None
        to = self.poll.identity if saw is Evidence.MOVED and self.poll is not None else ""
        self.moved = self.moved + 1 if to and to == self.moved_to else int(bool(to))
        self.moved_to = to
        if saw is Evidence.UNMOVED:
            self.last = self.poll


def _saw(poll: Poll, aimed: str) -> Evidence:
    if poll is None:
        return Evidence.GONE
    if isinstance(poll, Unreadable) or not poll.identity:
        return Evidence.UNREAD
    return Evidence.UNMOVED if poll.identity == aimed else Evidence.MOVED


def _settled(seen: _Run, *, ends: bool) -> Watched | None:
    if seen.gone >= GONE_POLLS:
        return Watched(Outcome.CONFIRMED, Evidence.GONE, None, seen.polls)
    # Unread never settles early: herdr says blocked for a few reads after a dialog has gone.
    if seen.moved >= MOVED_POLLS:
        ended = Outcome.UNCONFIRMED if ends else Outcome.CONFIRMED
        return Watched(ended, Evidence.MOVED, seen.poll, seen.polls)
    return None


def _spent(seen: _Run, *, ends: bool) -> Watched:
    # Report what the run mostly saw; a flicker qualifies it, never replaces it.
    if seen.saw is not Evidence.UNMOVED and seen.flickered:
        return Watched(Outcome.ACCEPTED, Evidence.FLICKER, None, seen.polls)
    if seen.saw is Evidence.UNREAD:
        return Watched(Outcome.ACCEPTED, Evidence.UNREAD, None, seen.polls)
    still = Outcome.UNCONFIRMED if ends else Outcome.ACCEPTED
    return Watched(still, Evidence.UNMOVED, seen.last, seen.polls)


def watch(read: Callable[[], Poll], *, aimed: str, ends: bool, pacing: Pacing) -> Watched:
    """Poll until the question the key was aimed at is gone, or the budget is."""
    interval, budget = (ANSWER_INTERVAL, ANSWER_BUDGET) if ends else (MOVE_INTERVAL, MOVE_BUDGET)
    deadline = pacing.clock() + budget
    seen = _Run()
    while True:
        poll = read()
        seen.note(_saw(poll, aimed), poll)
        settled = _settled(seen, ends=ends)
        if settled is not None:
            return settled
        if pacing.clock() >= deadline and not seen.take_grace():
            return _spent(seen, ends=ends)
        pacing.sleep(interval)
