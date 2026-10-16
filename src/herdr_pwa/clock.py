"""The one shape of a clock and of a sleep, so every seam that keeps time agrees on it."""

from collections.abc import Callable

# Seconds now: time.monotonic for intervals and deadlines, time.time for stamps.
type Clock = Callable[[], float]
# Wait this many seconds: time.sleep, or a test's clock moving forward.
type Sleep = Callable[[float], None]
