"""The cost helper: it passes linear work on any machine and fails quadratic work every time."""

import re

import pytest

from tests.costs import assert_linear

# Tried from every start, each run of "a" is read to its end for a "b", as a backtracking one is.
_QUADRATIC = re.compile(r"a+b")


def _letters(size: int) -> str:
    return "a" * size


def _linear(text: str) -> int:
    return sum(1 for char in text if char == "b")


def _quadratic(text: str) -> object:
    return _QUADRATIC.search(text)


def test_linear_work_passes() -> None:
    assert_linear(_letters, _linear, span=(1_000, 1_000_000))


def test_quadratic_work_fails_and_says_how_steep() -> None:
    with pytest.raises(AssertionError, match=r"^letters \d+\.\dx from [\d,]+ to [\d,]+$"):
        assert_linear(_letters, _quadratic, span=(100, 1_000_000), what="letters")


def test_work_that_never_reads_its_input_passes() -> None:
    assert_linear(_letters, lambda text: text[:10], span=(1_000, 64_000))
