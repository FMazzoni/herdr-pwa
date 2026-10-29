"""The one wait helper: it returns what it waited for, and it gives up at its ceiling, not before."""

import asyncio
import threading
import time

import pytest

from tests.waiting import GaveUpError, eventually, holds, until, until_async, watch, within

# Long enough that a probe answering on its third look is well inside it, on a loaded box too.
CEILING = 5.0
# Short, so a wait that never holds is over quickly; the assertion is on reaching it, not on speed.
SHORT = 0.2


class Counter:
    """A condition that comes true on its ``after``-th look, and counts every look."""

    def __init__(self, after: int) -> None:
        self.after = after
        self.looks = 0

    def __call__(self) -> str | None:
        self.looks += 1
        return "found" if self.looks >= self.after else None


def test_until_returns_the_first_thing_the_condition_found() -> None:
    probe = Counter(after=3)
    assert until(probe, CEILING) == "found"
    assert probe.looks == 3


def test_a_false_condition_is_not_found() -> None:
    looks: list[bool] = []

    def done() -> bool:
        looks.append(len(looks) >= 2)
        return looks[-1]

    assert until(done, CEILING) is True
    assert looks == [False, False, True]


def test_until_gives_up_at_the_ceiling_and_says_so() -> None:
    started = time.monotonic()
    with pytest.raises(GaveUpError, match=f"gave up after {SHORT}s"):
        until(lambda: None, SHORT)
    assert time.monotonic() - started >= SHORT


def test_a_wait_that_gives_up_fails_the_test_that_made_it() -> None:
    assert issubclass(GaveUpError, AssertionError)


def test_eventually_reports_rather_than_raises() -> None:
    probe = Counter(after=2)
    assert eventually(lambda: probe() is not None, CEILING) is True
    assert eventually(lambda: False, SHORT) is False


def test_holds_is_whether_the_condition_stayed_true_for_the_whole_window() -> None:
    started = time.monotonic()
    assert holds(lambda: True, SHORT) is True
    assert time.monotonic() - started >= SHORT
    probe = Counter(after=2)
    assert holds(lambda: probe() is None, CEILING) is False
    assert probe.looks == 2


def test_watch_looks_for_the_whole_window() -> None:
    looks: list[float] = []
    started = time.monotonic()
    watch(lambda: looks.append(time.monotonic()), SHORT)
    assert len(looks) > 1
    assert looks[-1] - started >= SHORT


def test_until_async_leaves_the_loop_free_to_do_the_work() -> None:
    async def scenario() -> str:
        done = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, done.set)
        return await until_async(lambda: "set" if done.is_set() else None, CEILING)

    assert asyncio.run(scenario()) == "set"


def test_until_async_gives_up_at_the_ceiling() -> None:
    def never() -> str | None:
        return None

    with pytest.raises(GaveUpError):
        asyncio.run(until_async(never, SHORT))


def test_within_hands_back_what_the_call_returned() -> None:
    assert within(lambda: "done", CEILING) == "done"


def test_within_raises_what_the_call_raised() -> None:
    def refuse() -> str:
        raise LookupError("refused")

    with pytest.raises(LookupError, match="refused"):
        within(refuse, CEILING)


def test_within_gives_up_on_a_call_still_going_at_the_ceiling() -> None:
    release = threading.Event()
    with pytest.raises(GaveUpError):
        within(lambda: release.wait(CEILING), SHORT)
    release.set()
