"""Waiting on a condition under a ceiling, never on a fixed sleep; the browser scripts import it too.

A fixed sleep stands in for "it has happened by now", which load makes false. These poll for the
thing itself, and the ceiling is only how long to look before calling it missing.
"""

import asyncio
import time
from collections.abc import Callable

STEP = 0.05


def until[T](probe: Callable[[], T | None], *, seconds: float, step: float = STEP) -> T:
    """Poll ``probe`` until it returns something other than None; fail after ``seconds``."""
    deadline = time.monotonic() + seconds
    while True:
        found = probe()
        if found is not None:
            return found
        if time.monotonic() > deadline:
            raise AssertionError(f"gave up after {seconds}s")
        time.sleep(step)


def eventually(done: Callable[[], bool], *, seconds: float, step: float = STEP) -> bool:
    """Poll ``done`` until it holds; whether it did within ``seconds``, for callers that report."""
    deadline = time.monotonic() + seconds
    while True:
        if done():
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(step)


async def until_async[T](probe: Callable[[], T | None], *, seconds: float, step: float = STEP) -> T:
    """``until`` for a coroutine: yields to the loop between looks, so the work can happen."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        found = probe()
        if found is not None:
            return found
        if loop.time() > deadline:
            raise AssertionError(f"gave up after {seconds}s")
        await asyncio.sleep(step)
