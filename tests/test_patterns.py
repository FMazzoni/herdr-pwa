"""Every pattern the app compiles, held to the examples its module records for it."""

import importlib
import pkgutil
import re
import time
from collections.abc import Iterator
from types import ModuleType
from typing import cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import herdr_pwa
from herdr_pwa.panetext import plain, without_escapes
from herdr_pwa.patterns import Rx
from herdr_pwa.validation import MODEL_ID
from tests.engine_seam import dialog_chunks

# The wire models are generated from herdr's schema and compile nothing of this app's.
_GENERATED = "herdr_pwa.engine.generated"
# A linear pattern reads this in well under a millisecond; a quadratic one took seconds.
_LONG = 5_000
# CPU time, so a loaded machine cannot inflate it: generous, and still far under a backtrack.
_CPU_SECONDS = 0.5
# Linear costs ~4x at four times the length and quadratic ~16x; 8x sits between, whatever the machine.
_SCALE = 4
_MAX_RATIO = 8.0
# Best of several runs, since noise only ever adds time.
_REPEATS = 5
# Below this the clock's grain and call overhead swamp the ratio, so the input grows first.
_FLOOR_SECONDS = 0.002
# Past this a pattern that is still under the floor costs nothing per character, so no ratio is read.
_MAX_LONG = 320_000
# A flood of 100k escapes took tens of seconds when quadratic; 5 s still catches that under a busy gate.
_FLOOD_CPU_SECONDS = 5.0

# Derandomized and kept off disk, so CI runs the same examples every time and leaves no database.
QUICK = settings(
    max_examples=200,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow],
)


def _modules() -> Iterator[ModuleType]:
    for info in pkgutil.walk_packages(herdr_pwa.__path__, "herdr_pwa."):
        if not info.name.startswith(_GENERATED):
            yield importlib.import_module(info.name)


def _namespace(module: ModuleType) -> dict[str, object]:
    # cast: a namespace holds whatever its module defines; each value is narrowed by isinstance.
    return cast("dict[str, object]", vars(module))


def _items(value: object) -> tuple[object, ...]:
    # cast: a tuple's items are objects until narrowed, which is all this asks of them.
    return cast("tuple[object, ...]", value) if isinstance(value, tuple) else (value,)


def _compiled(module: ModuleType) -> Iterator[tuple[str, re.Pattern[str]]]:
    """Every pattern *module* holds at its top level, alone or in a tuple."""
    for name, value in _namespace(module).items():
        items = _items(value)
        for at, item in enumerate(items):
            if isinstance(item, re.Pattern):
                # cast: this app compiles str patterns only.
                yield (name if len(items) == 1 else f"{name}[{at}]"), cast("re.Pattern[str]", item)


def _records(module: ModuleType) -> tuple[Rx, ...]:
    found = _items(_namespace(module).get("PATTERNS", ()))
    assert all(isinstance(rx, Rx) for rx in found), module.__name__
    return tuple(rx for rx in found if isinstance(rx, Rx))


MODULES = tuple(_modules())
NAMES = {id(p): f"{m.__name__}.{name}" for m in reversed(MODULES) for name, p in _compiled(m)}
RECORDS = tuple(rx for module in MODULES for rx in _records(module))
VALIDATORS = tuple(rx for rx in RECORDS if rx.validator)


def _name(rx: Rx) -> str:
    return NAMES.get(id(rx.pattern), rx.pattern.pattern[:30])


def test_every_compiled_pattern_carries_its_examples() -> None:
    recorded = {id(rx.pattern) for rx in RECORDS}
    assert sorted(name for key, name in NAMES.items() if key not in recorded) == []
    assert len(recorded) == len(RECORDS), "a pattern is recorded twice"


@pytest.mark.parametrize("rx", RECORDS, ids=_name)
def test_a_pattern_matches_what_it_says_and_nothing_it_refuses(rx: Rx) -> None:
    assert rx.matches, "a record shows what it matches"
    assert rx.rejects, "and what it does not"
    assert [text for text in rx.matches if rx.found(text) is None] == []
    assert [text for text in rx.rejects if rx.found(text) is not None] == []


@pytest.mark.parametrize("rx", VALIDATORS, ids=_name)
def test_a_validator_refuses_a_trailing_newline(rx: Rx) -> None:
    # ``$`` would admit one: https://docs.python.org/3/library/re.html#re.fullmatch
    assert rx.method == "fullmatch"
    assert [text for text in rx.matches if rx.found(text + "\n") is not None] == []


def _long_inputs(rx: Rx, length: int = _LONG) -> Iterator[str]:
    """Its own examples and their characters, repeated to a length no screen line reaches."""
    for example in (*rx.matches, *rx.rejects):
        if example:
            yield (example * (length // len(example) + 1))[:length]
    for char in sorted(set("".join((*rx.matches, *rx.rejects)))):
        yield char * length


def _read(rx: Rx, text: str) -> int:
    # A search pattern is read as sub, split and finditer read it: every match, not the first.
    if rx.method == "search":
        return sum(1 for _ in rx.pattern.finditer(text))
    return int(rx.found(text) is not None)


@pytest.mark.parametrize("rx", RECORDS, ids=_name)
def test_no_pattern_backtracks_on_long_input(rx: Rx) -> None:
    started = time.process_time()
    _ = sum(_read(rx, text) for text in _long_inputs(rx))
    assert time.process_time() - started < _CPU_SECONDS


def _best_times(rx: Rx, length: int) -> tuple[float, float]:
    """The least CPU time over the repeats to read its inputs at *length* and at the scaled length."""
    short, long = list(_long_inputs(rx, length)), list(_long_inputs(rx, length * _SCALE))
    best = [float("inf"), float("inf")]
    # Interleaved, so a slow spell on the machine lands on both sizes rather than one.
    for _ in range(_REPEATS):
        for at, texts in enumerate((short, long)):
            started = time.process_time()
            _ = sum(_read(rx, text) for text in texts)
            best[at] = min(best[at], time.process_time() - started)
    return best[0], best[1]


# Measured at 14-16x: each scans to the end from every start a run of its opener offers.
_SUPERLINEAR = frozenset(
    {
        "herdr_pwa.engine.dialog._KEY_PIECE",
        "herdr_pwa.engine.notices._REMINDER",
        "herdr_pwa.engine.notices._PEER",
        "herdr_pwa.writes.guard._NO_ECHO[2]",
    }
)


def _scaling_case(rx: Rx) -> object:
    reason = "quadratic on a run of its opener; reported to the owner, not yet rewritten"
    marks = [pytest.mark.xfail(strict=True, reason=reason)] if _name(rx) in _SUPERLINEAR else []
    return pytest.param(rx, marks=marks, id=_name(rx))


@pytest.mark.parametrize("rx", [_scaling_case(rx) for rx in RECORDS])
def test_no_pattern_costs_more_than_linear_time(rx: Rx) -> None:
    length = _LONG
    short, long = _best_times(rx, length)
    while short < _FLOOR_SECONDS and length < _MAX_LONG:
        length *= _SCALE
        short, long = _best_times(rx, length)
    if short < _FLOOR_SECONDS:
        return  # It bails out before reading the input, so there is no per-character cost to scale.
    assert long / short < _MAX_RATIO, f"{long / short:.1f}x at {length * _SCALE:,} characters"


@pytest.mark.parametrize("rx", VALIDATORS, ids=_name)
def test_what_a_validator_accepts_is_one_line(rx: Rx) -> None:
    @QUICK
    @given(st.from_regex(rx.pattern, fullmatch=True))
    def accepted(text: str) -> None:
        assert rx.found(text) is not None
        assert len(text.splitlines()) <= 1, "a value written to a header or a file stays one line"

    accepted()


@QUICK
@given(st.from_regex(MODEL_ID, fullmatch=True))
def test_a_model_id_holds_no_whitespace(model: str) -> None:
    # The doctor file is written on the promise that an id holds no whitespace.
    assert not any(c.isspace() for c in model)


# Escape floods: a string sequence's opener over and over, which a colour read could carry.
_FLOODS = ("\x1b]" * 20_000, "\x1bPx" * 15_000, "\x1b_" * 20_000)


@pytest.mark.parametrize("flood", _FLOODS, ids=["osc", "dcs", "apc"])
def test_an_escape_flood_is_read_in_linear_time(flood: str) -> None:
    started = time.process_time()
    _ = dialog_chunks(flood)
    _ = without_escapes(flood)
    _ = plain(flood)
    assert time.process_time() - started < _FLOOD_CPU_SECONDS
