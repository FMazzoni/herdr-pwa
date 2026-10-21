"""A Claude Code log read a page at a time by byte offset, backwards or past a frontier."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from herdr_pwa.engine.claude.turns import parse_claude_line
from herdr_pwa.engine.transcript import (
    MAX_PAGE_BYTES,
    NEWEST,
    Gather,
    HistoryState,
    TranscriptPage,
    TurnParser,
    Window,
)

_CHUNK: Final = 256 * 1024


def _lines_before(path: Path, before: int, budget: int) -> tuple[list[tuple[int, bytes]], int]:
    """Whole lines ending at byte *before*, oldest first, each with its offset."""
    start = max(0, before - budget)
    with path.open("rb") as handle:
        handle.seek(start)
        window = handle.read(before - start)
    if start > 0:
        head, sep, rest = window.partition(b"\n")
        if not sep:
            return [], start
        start += len(head) + 1
        window = rest
    found: list[tuple[int, bytes]] = []
    offset = start
    for line in window.split(b"\n"):
        if line.strip():
            found.append((offset, line))
        offset += len(line) + 1
    return found, start


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


def _take_backwards(
    lines: list[tuple[int, bytes]], parse: TurnParser, gather: Gather
) -> tuple[int | None, bool]:
    """Take the turns in *lines*, newest first, until *gather* is full."""
    oldest: int | None = None
    for offset, raw in reversed(lines):
        turn = parse(raw)
        if turn is None:
            continue
        oldest = offset
        if gather.take(turn, offset):
            return oldest, True
    return oldest, False


def _lines_reaching(path: Path, before: int, room: int) -> tuple[list[tuple[int, bytes]], int]:
    """`_lines_before`, widened past a record longer than the window, within *room* bytes."""
    window = _CHUNK * 4
    while True:
        lines, start = _lines_before(path, before, min(window, room))
        # One record longer than the window (an inline screenshot): widen it.
        if start != before or window >= room:
            return lines, start
        window *= 2


def _walk_back(path: Path, end: int, parse: TurnParser, gather: Gather) -> int:
    """Gather turns newest first from byte *end* backwards; return the page cursor."""
    max_bytes = gather.budget
    frontier = end
    cursor = end
    spent = 0
    full = False
    while frontier > 0 and not full and spent < max_bytes:
        lines, start = _lines_reaching(path, frontier, max_bytes - spent)
        if start == frontier:  # no progress within the budget; nothing more is readable
            break
        spent += frontier - start
        frontier = start
        taken, full = _take_backwards(lines, parse, gather)
        cursor = start if taken is None else taken
    if frontier == 0 and not full:
        cursor = 0
    return cursor


def read_page(
    path: Path, window: Window = NEWEST, *, parse: TurnParser | None = None
) -> TranscriptPage:
    """The *window*'s turns or messages, ending at byte ``before``, oldest first."""
    parse = parse or parse_claude_line
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


def read_since(path: Path, window: Window, *, parse: TurnParser | None = None) -> TranscriptPage:
    """Whatever has been appended past the *window*'s byte ``after``, oldest first."""
    parse = parse or parse_claude_line
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


def _take_forward(chunk: bytes, start: int, parse: TurnParser, gather: Gather) -> int:
    """Take the turns in *chunk* oldest first until *gather* is full; return the end offset."""
    end = start
    offset = start
    for raw in chunk.split(b"\n"):
        at = offset
        offset += len(raw) + 1
        end = offset
        if not raw.strip():
            continue
        if gather.take(parse(raw), at):
            break
    return end
