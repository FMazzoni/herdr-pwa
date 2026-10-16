"""Waiting on a condition under a ceiling, never on a fixed sleep; the browser scripts import it too.

A fixed sleep stands in for "it has happened by now", which load makes false. These poll for the
thing itself, and the ceiling is only how long to look before calling it missing. ``until`` is the
only place in tests/ and scripts/ that sleeps to wait; everything else here is built on it.
"""

import asyncio
import time
from collections.abc import Callable
from typing import Literal

STEP = 0.05

# What a condition hands back: the thing it waited for, or nothing yet.
type Found[T] = T | Literal[False] | None


class GaveUpError(AssertionError):
    """A condition never held within its ceiling; an AssertionError, so it fails the test."""


def until[T](condition: Callable[[], Found[T]], ceiling: float, *, step: float = STEP) -> T:
    """Poll ``condition`` until it returns something truthy and return that; fail at ``ceiling``."""
    deadline = time.monotonic() + ceiling
    while True:
        found = condition()
        if found:
            return found
        if time.monotonic() > deadline:
            raise GaveUpError(f"gave up after {ceiling}s")
        time.sleep(step)


def eventually(done: Callable[[], bool], ceiling: float, *, step: float = STEP) -> bool:
    """``until`` for callers that report: whether ``done`` held within ``ceiling``."""
    try:
        return until(done, ceiling, step=step)
    except GaveUpError:
        return False


def holds(condition: Callable[[], object], window: float, *, step: float = STEP) -> bool:
    """Whether ``condition`` stayed true for the whole ``window``: it is looked at throughout."""
    return not eventually(lambda: not condition(), window, step=step)


def watch(look: Callable[[], object], window: float, *, step: float = STEP) -> None:
    """Call ``look`` throughout ``window`` and only then return: an observation, not a wait."""

    def never() -> Literal[False]:
        look()
        return False

    _ = eventually(never, window, step=step)


async def until_async[T](
    condition: Callable[[], Found[T]], ceiling: float, *, step: float = STEP
) -> T:
    """``until`` for a coroutine, polled off the loop so the loop is free to do the work."""
    return await asyncio.to_thread(until, condition, ceiling, step=step)
