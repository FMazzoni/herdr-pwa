"""What work costs, asserted as growth between two input sizes and never as seconds.

Seconds measure the machine as much as the code: a busy gate slows a linear scan past any bound
tight enough to catch a quadratic one. Growth does not: reading four times the input costs about
four times as much when linear and sixteen when quadratic, on any machine. ``assert_linear`` is the
only place in tests/ that times work; waiting on a condition is ``tests.waiting``'s job.
"""

import gc
import time
from collections.abc import Callable
from typing import Final

# Linear costs ~4x at four times the size and quadratic ~16x; 8x sits between, whatever the machine.
SCALE: Final = 4
LINEAR: Final = 8.0
# Best of several runs, since noise only ever adds time; two runs within 10% already settle it.
_REPEATS: Final = 5
_AGREEING: Final = 2
_AGREEMENT: Final = 1.1
# Below this the clock's grain and call overhead swamp the ratio, so the input grows first.
_FLOOR_SECONDS: Final = 0.0005
# Over the bound, measured again: same sizes for a slow spell, the next for a cache step.
_AGAIN_AT: Final = (1, SCALE)


def assert_linear[T](
    build: Callable[[int], T],
    work: Callable[[T], object],
    *,
    span: tuple[int, int],
    what: str = "",
) -> None:
    """Fail unless ``work`` on ``build(n)`` costs under ``LINEAR`` times more at ``SCALE`` times n.

    Args:
        build: makes the input of a size; it is never timed.
        work: what is timed, on one input.
        span: the smallest size to try, grown by ``SCALE`` while the work is too cheap to time, and
            the most it grows to: past that the work costs nothing per unit, and the check passes.
        what: names the input in the failure, when one test checks several.
    """
    size = _readable(build, work, *span)
    short, long = _best_times(build, work, size)
    # Still under the floor: it bails out before reading the input, so no per-unit cost scales.
    if short < _FLOOR_SECONDS:
        return
    ratio = long / short
    for step in _AGAIN_AT:
        if ratio < LINEAR:
            break
        again, longer = _best_times(build, work, size * step)
        ratio = min(ratio, longer / again)
    assert ratio < LINEAR, f"{what} {ratio:.1f}x from {size:,} to {size * SCALE:,}".lstrip()


def _readable[T](
    build: Callable[[int], T], work: Callable[[T], object], size: int, most: int
) -> int:
    # One run picks the size; only the size picked is repeated.
    while size < most and _cpu(work, build(size)) < _FLOOR_SECONDS:
        size *= SCALE
    return size


def _best_times[T](
    build: Callable[[int], T], work: Callable[[T], object], size: int
) -> tuple[float, float]:
    """The least CPU time over the repeats at ``size`` and at ``SCALE`` times it."""
    inputs = (build(size), build(size * SCALE))
    runs: tuple[list[float], list[float]] = ([], [])
    # Interleaved, so a slow spell on the machine lands on both sizes rather than one.
    while len(runs[0]) < _REPEATS and not all(_agree(each) for each in runs):
        for given, each in zip(inputs, runs, strict=True):
            each.append(_cpu(work, given))
    return min(runs[0]), min(runs[1])


def _cpu[T](work: Callable[[T], object], given: T) -> float:
    # This thread's CPU time, so threads a test left running add none; GC off, as in timeit.
    collecting = gc.isenabled()
    gc.disable()
    try:
        started = time.thread_time()
        _ = work(given)
        return time.thread_time() - started
    finally:
        if collecting:
            gc.enable()


def _agree(runs: list[float]) -> bool:
    return len(runs) >= _AGREEING and max(runs[-_AGREEING:]) <= min(runs) * _AGREEMENT
