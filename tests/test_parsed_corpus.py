"""Every screen in the capture corpus, parsed, against the question it parsed to when it was frozen.

A grammar change is meant to teach the parser one more screen. This walk is how it proves it
taught nothing else: each fixture's question, rows, hints, shape and control token are pinned,
so a new rule that also reads a transcript line or a status bar as an answer fails here by name.

Regenerate after a deliberate change with ``uv run python -m tests.test_parsed_corpus``, then
read the diff of the golden file: every changed entry must be one the change set out to change.
"""

import functools
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from herdr_pwa.engine import BlockedPrompt
from herdr_pwa.panetext import as_text_read
from herdr_pwa.writes.keys import control_token
from tests import _foreign

FIXTURES = _foreign.REPO / "tests" / "fixtures"
GOLDEN = FIXTURES / "parsed-corpus.json"
FOREIGN_PREFIX = "foreign/"
COLOUR_SUFFIX = "#ansi"

# The screens this repo captured, as text reads; the four sources of a capture each count.
_TEXT_GLOBS = ("capture-*/*.txt", "hermes/*.txt")
# Colour reads, parsed as the app parses them: the text read of the same bytes, colour beside it.
_ANSI_GLOBS = ("capture-*/*.ansi", "ansi/*.ansi", "rig/*.ansi")
# herdr's explain output sits beside each capture and is a report, not a screen.
_NOT_SCREENS = frozenset({"explain.txt"})

_DOCUMENT = TypeAdapter(dict[str, JsonValue])


def _local() -> Iterator[tuple[str, Path]]:
    for pattern in _TEXT_GLOBS:
        for path in sorted(FIXTURES.glob(pattern)):
            if path.name not in _NOT_SCREENS:
                yield str(path.relative_to(_foreign.REPO)), path
    for pattern in _ANSI_GLOBS:
        for path in sorted(FIXTURES.glob(pattern)):
            yield str(path.relative_to(_foreign.REPO)) + COLOUR_SUFFIX, path


def _foreign_screens(root: Path) -> Iterator[tuple[str, Path]]:
    for clone, source in _foreign.sources().items():
        folder = root / clone / source["captures"]
        for path in sorted(folder.glob(source["glob"])):
            suffix = COLOUR_SUFFIX if path.suffix == ".ansi" else ""
            yield f"{FOREIGN_PREFIX}{clone}/{path.name}{suffix}", path


def screens(root: Path) -> dict[str, Path]:
    """Every screen the walk parses, by the id its golden entry is filed under."""
    return dict([*_local(), *_foreign_screens(root)])


def parse(key: str, path: Path) -> BlockedPrompt:
    """The prompt *path* parses to, read the way its kind of capture is read."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    if key.endswith(COLOUR_SUFFIX):
        return BlockedPrompt.parse(as_text_read(raw), ansi=raw)
    return BlockedPrompt.parse(raw)


def described(prompt: BlockedPrompt) -> dict[str, JsonValue]:
    """What a screen parsed to, as far as anything downstream can tell."""
    step = prompt.stepper
    return {
        "shape": prompt.shape.value,
        "question": prompt.question,
        "body": prompt.body,
        "options": [[o.number, o.label, o.checked] for o in prompt.options],
        "hints": [[h.key, h.verb] for h in prompt.hints],
        "handoffs": [[h.key, h.verb] for h in prompt.handoffs],
        "focus": prompt.focus,
        "stepper": None if step is None else [step.answered, *step.steps],
        "corroborated": prompt.corroborated,
        "identity": prompt.identity,
        "token": control_token(prompt),
    }


def snapshot(root: Path) -> dict[str, JsonValue]:
    """The whole corpus, parsed now."""
    return {key: described(parse(key, path)) for key, path in screens(root).items()}


@functools.cache
def _golden() -> dict[str, JsonValue]:
    return _DOCUMENT.validate_json(GOLDEN.read_bytes())


@pytest.fixture(scope="module")
def corpus_root(request: pytest.FixtureRequest) -> Path:
    option: object = request.config.getoption("--foreign-root")
    root = Path(option) if isinstance(option, str) and option else _foreign.DEFAULT_ROOT
    # Never a skip: without the clones two thirds of the screens would drop out silently.
    if found := _foreign.problems(root):
        pytest.fail(_foreign.explain(root, found), pytrace=False)
    return root


def test_every_screen_is_pinned_and_nothing_pinned_has_gone(corpus_root: Path) -> None:
    assert set(screens(corpus_root)) == set(_golden())


def test_no_screen_parses_to_anything_but_what_it_was_pinned_to(corpus_root: Path) -> None:
    golden = _golden()
    changed = {
        key: {"was": golden.get(key), "now": now}
        for key, now in snapshot(corpus_root).items()
        if golden.get(key) != now
    }
    assert not changed, changed


def test_the_walk_reaches_every_kind_of_capture(corpus_root: Path) -> None:
    keys = set(screens(corpus_root))
    for prefix in ("tests/fixtures/capture-", "tests/fixtures/hermes/", "tests/fixtures/rig/"):
        assert any(key.startswith(prefix) for key in keys), prefix
    for clone in _foreign.sources():
        assert any(key.startswith(f"{FOREIGN_PREFIX}{clone}/") for key in keys), clone


if __name__ == "__main__":
    _ = GOLDEN.write_bytes(_DOCUMENT.dump_json(snapshot(_foreign.default_root()), indent=2) + b"\n")
