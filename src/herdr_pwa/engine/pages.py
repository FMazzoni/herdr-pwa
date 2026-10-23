"""A JSONL log read a page at a time by byte offset, backwards or past a frontier."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final, NamedTuple

from herdr_pwa.engine.transcript import (
    MAX_HISTORY_BYTES,
    MAX_PAGE_BYTES,
    NEWEST,
    Gather,
    HistoryState,
    TranscriptPage,
    TranscriptTurn,
    Window,
)

# Every turn one line holds, oldest first, given the byte its first character sits at.
type LineTurns = Callable[[int, bytes], Sequence[TranscriptTurn]]

_CHUNK: Final = 256 * 1024


class Lines(NamedTuple):
    """The whole lines a byte window holds, each with its offset, and where the window begins."""

    items: list[tuple[int, bytes]]
    start: int


def contained(path: Path, base: Path) -> Path | None:
    """*path* resolved, then re-checked to still be a file inside *base*."""
    # A symlink satisfies any comparison of the strings we were handed, so resolve first.
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_relative_to(base) or not resolved.is_file():
        return None
    return resolved


def lines_between(path: Path, start: int, end: int) -> Lines:
    """The whole lines a byte window holds, oldest first, and where its first whole line begins."""
    if end <= start:
        return Lines([], start)
    with path.open("rb") as handle:
        handle.seek(start)
        window = handle.read(end - start)
    if start > 0:
        head, sep, rest = window.partition(b"\n")
        if not sep:
            return Lines([], start)
        start += len(head) + 1
        window = rest
    found: list[tuple[int, bytes]] = []
    offset = start
    for line in window.split(b"\n"):
        if line.strip():
            found.append((offset, line))
        offset += len(line) + 1
    return Lines(found, start)


def read_page(path: Path, window: Window = NEWEST, *, parse: LineTurns) -> TranscriptPage:
    """The *window*'s turns or messages, ending at byte ``before``, oldest first."""
    try:
        size = path.stat().st_size
    except OSError:
        return TranscriptPage(state=HistoryState.unreadable)
    before = window.before
    end = _complete_end(path, size if before is None else max(0, min(before, size)))
    gather = Gather(window)
    cursor = _walk_back(path, end, parse, gather)
    turns = gather.turns
    turns.reverse()
    return TranscriptPage(
        turns=turns,
        cursor=cursor,
        size=size,
        has_more=cursor > 0,
        end=end,
        state=HistoryState.ok if turns else HistoryState.empty,
        log=path.stem,
    )


def read_since(path: Path, window: Window, *, parse: LineTurns) -> TranscriptPage:
    """Whatever has been appended at or past the *window*'s byte ``after``, oldest first."""
    try:
        size = path.stat().st_size
    except OSError:
        return TranscriptPage(state=HistoryState.unreadable)
    start = max(0, window.after or 0)
    if start > size:
        return TranscriptPage(
            size=size, end=size, reset=True, state=HistoryState.empty, log=path.stem
        )
    with path.open("rb") as handle:
        handle.seek(start)
        chunk = handle.read(min(window.budget, size - start))
    tail = chunk.rfind(b"\n")
    if tail < 0:
        # No whole line yet, so the cursor must not move past bytes never delivered.
        return TranscriptPage(
            size=size,
            end=start,
            has_more=size > start,
            state=HistoryState.empty,
            log=path.stem,
        )
    gather = Gather(window)
    end = _take_forward(chunk[:tail], start, parse, gather)
    return TranscriptPage(
        turns=gather.turns,
        cursor=start,
        size=size,
        end=end,
        has_more=end < size,
        state=HistoryState.ok if gather.turns else HistoryState.empty,
        log=path.stem,
    )


def _complete_end(path: Path, end: int, max_bytes: int = MAX_PAGE_BYTES) -> int:
    """*end*, pulled back to just past the last line that was written WHOLE."""
    if end <= 0:
        return 0
    with path.open("rb") as handle:
        handle.seek(end - 1)
        if handle.read(1) == b"\n":
            return end
        frontier = end
        while frontier > 0 and end - frontier < max_bytes:
            start = max(0, frontier - _CHUNK)
            handle.seek(start)
            cut = handle.read(frontier - start).rfind(b"\n")
            if cut >= 0:
                return start + cut + 1
            frontier = start
    # No newline means nothing delivered; past the budget one record is too long to scan back over.
    return 0 if frontier == 0 else end


def _walk_back(path: Path, end: int, parse: LineTurns, gather: Gather) -> int:
    """Gather turns newest first from byte *end* backwards; return the page cursor."""
    max_bytes = gather.budget
    frontier = end
    cursor = end
    spent = 0
    full = False
    while frontier > 0 and not full and spent < max_bytes:
        window = _lines_reaching(path, frontier, max_bytes - spent)
        if window.start == frontier:  # no progress within the budget; nothing more is readable
            break
        spent += frontier - window.start
        frontier = window.start
        taken, full = _take_backwards(window.items, parse, gather)
        cursor = window.start if taken is None else taken
    if frontier == 0 and not full:
        cursor = 0
    return cursor


def _lines_reaching(path: Path, before: int, room: int) -> Lines:
    """`lines_between`, widened past a record longer than the window, within *room* bytes."""
    reach = min(_CHUNK * 4, room)
    while True:
        lines = lines_between(path, max(0, before - reach), before)
        if lines.start != before or reach >= max(room, MAX_HISTORY_BYTES):
            return lines
        # One record longer than the budget (an inline screenshot) widens to a hard ceiling, not lost.
        reach = min(reach * 2, room if reach < room else MAX_HISTORY_BYTES)


def _take_backwards(
    lines: list[tuple[int, bytes]], parse: LineTurns, gather: Gather
) -> tuple[int | None, bool]:
    """Take the turns in *lines*, newest first, a line at a time, until *gather* is full."""
    oldest: int | None = None
    full = False
    for offset, raw in reversed(lines):
        turns = parse(offset, raw)
        if not turns:
            continue
        oldest = offset
        # A line is never split: every block it holds belongs to the same page.
        for turn in reversed(turns):
            full = gather.take(turn, offset)
        if full:
            return oldest, True
    return oldest, False


def _take_forward(chunk: bytes, start: int, parse: LineTurns, gather: Gather) -> int:
    """Take the turns in *chunk* oldest first until *gather* is full; return the end offset."""
    end = start
    offset = start
    for raw in chunk.split(b"\n"):
        at = offset
        offset += len(raw) + 1
        end = offset
        if not raw.strip():
            continue
        full = False
        for turn in parse(at, raw):
            full = gather.take(turn, at)
        if full:
            break
    return end
