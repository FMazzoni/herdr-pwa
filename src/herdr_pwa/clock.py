"""Time: the wall and monotonic clocks, a sleep, and the one wording of a span of it."""

import time
from collections.abc import Callable
from typing import Final, NewType

# Seconds since the epoch, from time.time: a stamp to write down, or to say how long ago.
WallTime = NewType("WallTime", float)
# Seconds on time.monotonic: only the difference of two readings means anything.
MonoTime = NewType("MonoTime", float)
# Two clock types, so a wall clock cannot be passed where intervals and deadlines are measured.
type WallClock = Callable[[], WallTime]
type MonoClock = Callable[[], MonoTime]
# Wait this many seconds: time.sleep, or a test's clock moving forward.
type Sleep = Callable[[float], None]


def wall_now() -> WallTime:
    """The wall clock: seconds since the epoch."""
    return WallTime(time.time())


def mono_now() -> MonoTime:
    """The monotonic clock: seconds from an arbitrary start, never stepped back."""
    return MonoTime(time.monotonic())


_MINUTE: Final = 60
_HOUR: Final = 3600
_DAY: Final = 86400


def _whole(seconds: float) -> int:
    # A stamp a little ahead of this clock is a skewed clock, not a span to word as negative.
    return max(0, int(seconds))


def duration(seconds: float) -> str:
    """A span in full: 40s, 4m, 1h 12m, 2d 7h."""
    whole = _whole(seconds)
    if whole < _HOUR:
        return brief(whole)
    if whole < _DAY:
        return f"{whole // _HOUR}h {whole % _HOUR // _MINUTE}m"
    return f"{whole // _DAY}d {whole % _DAY // _HOUR}h"


def elapsed(seconds: float) -> str:
    """A span to the second under an hour (42s, 4m 12s, 2m); past that, as duration words it."""
    whole = _whole(seconds)
    if whole < _MINUTE or whole >= _HOUR:
        return duration(whole)
    minutes, rest = divmod(whole, _MINUTE)
    return f"{minutes}m {rest}s" if rest else f"{minutes}m"


def brief(seconds: float) -> str:
    """A span at a glance, in its largest whole unit: 40s, 4m, 1h, 2d."""
    whole = _whole(seconds)
    for size, unit in ((_DAY, "d"), (_HOUR, "h"), (_MINUTE, "m")):
        if whole >= size:
            return f"{whole // size}{unit}"
    return f"{whole}s"


def ago(seconds: float) -> str:
    """How long ago something was, at a glance: 40s ago, 4m ago, 2d ago."""
    return f"{brief(seconds)} ago"


def _counted(count: int, unit: str) -> str:
    return f"{count} {unit}{'' if count == 1 else 's'}"


def duration_said(seconds: float) -> str:
    """A span as VoiceOver should say it."""
    whole = _whole(seconds)
    if whole < _MINUTE:
        return "under a minute"
    if whole < _HOUR:
        return _counted(whole // _MINUTE, "minute")
    if whole < _DAY:
        return f"{_counted(whole // _HOUR, 'hour')} {_counted(whole % _HOUR // _MINUTE, 'minute')}"
    return f"{_counted(whole // _DAY, 'day')} {_counted(whole % _DAY // _HOUR, 'hour')}"
