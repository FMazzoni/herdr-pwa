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

from pydantic import JsonValue, TypeAdapter

from herdr_pwa.engine import BlockedPrompt
from herdr_pwa.panetext import as_text_read
from herdr_pwa.writes.keys import control_token

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures"
GOLDEN = FIXTURES / "parsed-corpus.json"
FOREIGN_PREFIX = "foreign/"
COLOUR_SUFFIX = "#ansi"
ESCAPE = "\x1b"

# The screens this repo captured, as text reads; the four sources of a capture each count.
_TEXT_GLOBS = ("capture-*/*.txt", "hermes/*.txt")
# collie's claude screens, vendored under MIT and filed by the id the ledger gives them.
_VENDORED_GLOB = "foreign/collie/*.txt"
# Colour reads, parsed as the app parses them: the text read of the same bytes, colour beside it.
_ANSI_GLOBS = ("capture-*/*.ansi", "ansi/*.ansi", "rig/*.ansi")
# herdr's explain output sits beside each capture and is a report, not a screen.
_NOT_SCREENS = frozenset({"explain.txt"})

_DOCUMENT = TypeAdapter(dict[str, JsonValue])


def _local() -> Iterator[tuple[str, Path]]:
    for pattern in _TEXT_GLOBS:
        for path in sorted(FIXTURES.glob(pattern)):
            if path.name not in _NOT_SCREENS:
                yield str(path.relative_to(REPO)), path
    for pattern in _ANSI_GLOBS:
        for path in sorted(FIXTURES.glob(pattern)):
            yield str(path.relative_to(REPO)) + COLOUR_SUFFIX, path


def _vendored() -> Iterator[tuple[str, Path]]:
    for path in sorted(FIXTURES.glob(_VENDORED_GLOB)):
        yield str(path.relative_to(FIXTURES)), path


def screens() -> dict[str, Path]:
    """Every screen the walk parses, by the id its golden entry is filed under."""
    return dict([*_local(), *_vendored()])


def parse(key: str, path: Path) -> BlockedPrompt:
    """The prompt *path* parses to, read the way the app reads a blocked pane."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    # A text capture with escapes still in it is a colour read under another name.
    if not key.endswith(COLOUR_SUFFIX) and ESCAPE not in raw:
        return BlockedPrompt.parse(raw)
    # herdr strips a text read before the app sees it; the colour read is the capture itself.
    return BlockedPrompt.from_reads(as_text_read(raw), lambda: raw)


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


def snapshot() -> dict[str, JsonValue]:
    """The whole corpus, parsed now."""
    return {key: described(parse(key, path)) for key, path in screens().items()}


@functools.cache
def _golden() -> dict[str, JsonValue]:
    return _DOCUMENT.validate_json(GOLDEN.read_bytes())


def test_every_screen_is_pinned_and_nothing_pinned_has_gone() -> None:
    assert set(screens()) == set(_golden())


def test_no_screen_parses_to_anything_but_what_it_was_pinned_to() -> None:
    golden = _golden()
    changed = {
        key: {"was": golden.get(key), "now": now}
        for key, now in snapshot().items()
        if golden.get(key) != now
    }
    assert not changed, changed


def test_no_screen_reaches_the_grammar_with_its_escapes_in() -> None:
    raw = {key for key, path in screens().items() if ESCAPE in parse(key, path).raw}
    assert not raw, sorted(raw)


def _padded(text: str) -> str:
    """*text* as a read that keeps the padding to the pane's width, as tmux's capture does."""
    rows = text.splitlines()
    width = max((len(row) for row in rows), default=0)
    return "\n".join(row.ljust(width) for row in rows)


def test_padding_a_read_to_the_pane_width_changes_nothing_it_parses_to() -> None:
    moved: dict[str, list[str]] = {}
    for key, path in screens().items():
        raw = path.read_text(encoding="utf-8", errors="replace")
        text = raw if not key.endswith(COLOUR_SUFFIX) and ESCAPE not in raw else as_text_read(raw)
        trimmed = described(BlockedPrompt.parse(text))
        padded = described(BlockedPrompt.parse(_padded(text)))
        # The token digests the raw read when nothing was asked, and padding is in the raw read.
        fields = [f for f in trimmed if f != "token" and trimmed[f] != padded[f]]
        if fields:
            moved[key] = fields
    assert not moved, moved


def test_the_walk_reaches_every_kind_of_capture() -> None:
    keys = set(screens())
    for prefix in ("tests/fixtures/capture-", "tests/fixtures/hermes/", "tests/fixtures/rig/"):
        assert any(key.startswith(prefix) for key in keys), prefix
    assert any(key.startswith(FOREIGN_PREFIX) for key in keys), FOREIGN_PREFIX


if __name__ == "__main__":
    _ = GOLDEN.write_bytes(_DOCUMENT.dump_json(snapshot(), indent=2) + b"\n")
