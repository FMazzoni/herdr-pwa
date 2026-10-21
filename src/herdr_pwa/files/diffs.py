"""One file's change against HEAD, as numbered lines with a kind each, under a cap."""

import re
from dataclasses import dataclass
from typing import Final

MAX_LINES: Final = 3000
LINE_CHARS: Final = 2000
BINARY: Final = "The file is binary; there is no text to compare."
_HUNK: Final = re.compile(
    r"""
    @@\ -(?P<old>\d+)(?:,\d+)?  # where the hunk starts in the old file, and its length
    \ \+(?P<new>\d+)(?:,\d+)?\ @@  # and in the new one
    (?P<heading>.*)  # the function or section git names it after
    """,
    re.VERBOSE,
)


@dataclass(frozen=True, slots=True)
class DiffLine:
    """One row: ``kind`` is add, del, same or hunk; a number is 0 where that side has no line."""

    kind: str
    old: int
    new: int
    text: str
    cut: int = 0


@dataclass(frozen=True, slots=True)
class FileDiff:
    """A file's rows, how many rows the cap left out, and a note when git showed no text."""

    lines: tuple[DiffLine, ...]
    more: int = 0
    note: str = ""

    @property
    def first_changed(self) -> int:
        """The new-side number of the first added line, or of the first hunk, for "open file"."""
        added = next((d.new for d in self.lines if d.kind == "add"), 0)
        return added or next((d.new for d in self.lines if d.kind == "hunk"), 0)


def _cut(text: str) -> tuple[str, int]:
    return (text[:LINE_CHARS], len(text) - LINE_CHARS) if len(text) > LINE_CHARS else (text, 0)


class _Walk:
    def __init__(self) -> None:
        self.old = 0
        self.new = 0
        self.rows: list[DiffLine] = []

    def hunk(self, found: re.Match[str]) -> None:
        self.old, self.new = int(found["old"]), int(found["new"])
        self.rows.append(DiffLine("hunk", 0, self.new, found["heading"].strip()))

    def body(self, raw: str) -> None:
        mark, text = raw[:1], raw[1:]
        shown, cut = _cut(text)
        if mark == "+":
            self.rows.append(DiffLine("add", 0, self.new, shown, cut))
            self.new += 1
        elif mark == "-":
            self.rows.append(DiffLine("del", self.old, 0, shown, cut))
            self.old += 1
        elif mark == " ":
            self.rows.append(DiffLine("same", self.old, self.new, shown, cut))
            self.old += 1
            self.new += 1


def parse(text: str) -> FileDiff:
    """Unified diff text, as rows; a binary change is a note, not rows."""
    walk = _Walk()
    for raw in text.splitlines():
        found = _HUNK.fullmatch(raw)
        if found:
            walk.hunk(found)
        elif raw.startswith("Binary files") and not walk.rows:
            return FileDiff((), note=BINARY)
        elif walk.rows:
            # Everything before the first hunk is the file header, which the page already says.
            walk.body(raw)
    rows = walk.rows
    return FileDiff(tuple(rows[:MAX_LINES]), max(0, len(rows) - MAX_LINES))


def added(text: str) -> FileDiff:
    """A file git has never seen: every line is new."""
    lines = text.split("\n")
    lines = lines[:-1] if lines and not lines[-1] else lines
    rows = [DiffLine("add", 0, n, *_cut(t)) for n, t in enumerate(lines[:MAX_LINES], start=1)]
    return FileDiff(tuple(rows), max(0, len(lines) - MAX_LINES))
