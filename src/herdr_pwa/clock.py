"""Time: the one shape of a clock and of a sleep, and the one wording of a span of it."""

from collections.abc import Callable
from typing import Final

# Seconds now: time.monotonic for intervals and deadlines, time.time for stamps.
type Clock = Callable[[], float]
# Wait this many seconds: time.sleep, or a test's clock moving forward.
type Sleep = Callable[[float], None]

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
