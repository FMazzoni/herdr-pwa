"""Every pattern the app compiles, held to the examples its module records for it."""

import importlib
import pkgutil
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial
from types import ModuleType
from typing import cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import herdr_pwa
from herdr_pwa.panetext import plain, without_escapes
from herdr_pwa.patterns import Rx
from herdr_pwa.validation import MODEL_ID
from herdr_pwa.writes.guard import no_echo_notice
from tests.engine_seam import dialog_chunks, injected

# The wire models are generated from herdr's schema and compile nothing of this app's.
_GENERATED = "herdr_pwa.engine.generated"
# A linear pattern reads this in well under a millisecond; a quadratic one took seconds.
_LONG = 5_000
# CPU time, so a loaded machine cannot inflate it: generous, and still far under a backtrack.
_CPU_SECONDS = 0.5
# Linear costs ~4x at four times the length and quadratic ~16x; 8x sits between, whatever the machine.
_SCALE = 4
_MAX_RATIO = 8.0
# Best of several runs, since noise only ever adds time; two runs within 10% already settle it.
_REPEATS = 5
_AGREEING = 2
_AGREEMENT = 1.1
# Below this the clock's grain and call overhead swamp the ratio, so the input grows first.
_FLOOR_SECONDS = 0.0005
# Past this a shape still under the floor costs nothing per character, so no ratio is read.
_MAX_LONG = 80_000
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


def _runs(examples: tuple[str, ...], length: int) -> Iterator[str]:
    for example in examples:
        if example:
            yield (example * (length // len(example) + 1))[:length]
    for char in sorted(set("".join(examples))):
        yield char * length


def _long_inputs(
    examples: tuple[str, ...], length: int = _LONG, ends: tuple[str, ...] = ("",)
) -> Iterator[str]:
    """The examples and their characters, repeated to a length no screen line reaches, then each end."""
    return (run + end for run in _runs(examples, length) for end in ends)


def _read(rx: Rx, text: str) -> int:
    # A search pattern is read as sub, split and finditer read it: every match, not the first.
    if rx.method == "search":
        return sum(1 for _ in rx.pattern.finditer(text))
    return int(rx.found(text) is not None)


@pytest.mark.parametrize("rx", RECORDS, ids=_name)
def test_no_pattern_backtracks_on_long_input(rx: Rx) -> None:
    started = time.process_time()
    _ = sum(_read(rx, text) for text in _long_inputs((*rx.matches, *rx.rejects)))
    assert time.process_time() - started < _CPU_SECONDS


@dataclass(frozen=True, slots=True)
class _Reader:
    read: Callable[[str], object]
    examples: tuple[str, ...]
    # What closes a long run, for a reader that only does work once the run can close.
    ends: tuple[str, ...] = ("",)


def _pattern_reader(rx: Rx) -> object:
    return pytest.param(_Reader(partial(_read, rx), (*rx.matches, *rx.rejects)), id=_name(rx))


# Read with str methods rather than a pattern, on the shapes that made their regexes quadratic.
_TAG_EXAMPLES = (
    "<system-reminder>x</system-reminder>",
    "a<system-reminder>\nmany\n</system-reminder>b",
    "<system-reminder>open",
    '<agent-message from="a">\nhi\n</agent-message>',
    '<cross-session-message from="b">x</cross-session-message>',
    '<agent-message from="a">x</cross-session-message>',
    "<agent-messages>x</agent-messages>",
    "<agent-messages</agent-message>",
    "<agent-messages",
)
_TAG_READER = pytest.param(
    _Reader(partial(injected, None, human=False), _TAG_EXAMPLES, ("", "></agent-message>")),
    id="notices.injected",
)
_PASSPHRASE_EXAMPLES = (
    "Enter passphrase for key '/k':",
    "Enter passphrase",
    "enter passphrase for:",
)
_PASSPHRASE_READER = pytest.param(
    _Reader(no_echo_notice, _PASSPHRASE_EXAMPLES), id="guard.no_echo_notice"
)


def _shapes(reader: _Reader) -> Iterator[tuple[str, str]]:
    units = (
        *(example for example in reader.examples if example),
        *sorted(set("".join(reader.examples))),
    )
    return ((unit, end) for unit in units for end in reader.ends)


def _built(shape: tuple[str, str], length: int) -> str:
    unit, end = shape
    return (unit * (length // len(unit) + 1))[:length] + end


def _cpu(reader: _Reader, text: str) -> float:
    started = time.process_time()
    _ = reader.read(text)
    return time.process_time() - started


def _agree(runs: list[float]) -> bool:
    return len(runs) >= _AGREEING and max(runs[-_AGREEING:]) <= min(runs) * _AGREEMENT


def _best_times(reader: _Reader, shape: tuple[str, str], length: int) -> tuple[float, float]:
    """The least CPU time over the repeats to read *shape* at *length* and at the scaled length."""
    sizes = [_built(shape, size) for size in (length, length * _SCALE)]
    runs: tuple[list[float], list[float]] = ([], [])
    # Interleaved, so a slow spell on the machine lands on both sizes rather than one.
    while len(runs[0]) < _REPEATS and not all(_agree(each) for each in runs):
        for text, each in zip(sizes, runs, strict=True):
            each.append(_cpu(reader, text))
    return min(runs[0]), min(runs[1])


def _shape_ratio(reader: _Reader, shape: tuple[str, str]) -> tuple[float, int] | None:
    length = _LONG
    # One run picks the length; only the length measured is repeated.
    while length < _MAX_LONG and _cpu(reader, _built(shape, length)) < _FLOOR_SECONDS:
        length *= _SCALE
    short, long = _best_times(reader, shape, length)
    # Still under the floor: it bails out before reading the input, so no per-character cost scales.
    if short < _FLOOR_SECONDS:
        return None
    if long / short < _MAX_RATIO:
        return long / short, length
    # Quadratic is steep at every size; a cache filling up, or a slow spell, is one step.
    again, longer = _best_times(reader, shape, length * _SCALE)
    return min(long / short, longer / again), length


def _ratio(reader: _Reader) -> tuple[float, int] | None:
    """The steepest growth over its shapes, each timed alone so a cheap quadratic one is not diluted."""
    found = (ratio for shape in _shapes(reader) if (ratio := _shape_ratio(reader, shape)))
    return max(found, default=None)


@pytest.mark.parametrize(
    "reader", [*map(_pattern_reader, RECORDS), _TAG_READER, _PASSPHRASE_READER]
)
def test_no_pattern_costs_more_than_linear_time(reader: _Reader) -> None:
    measured = _ratio(reader)
    if measured is not None:
        ratio, length = measured
        assert ratio < _MAX_RATIO, f"{ratio:.1f}x at {length * _SCALE:,} characters"


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
