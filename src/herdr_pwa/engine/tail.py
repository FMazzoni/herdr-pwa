"""The open stretch of each viewed Claude transcript, parsed once and grown by what is appended."""

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from herdr_pwa.engine import transcript
from herdr_pwa.engine.transcript import (
    MAX_HISTORY_BYTES,
    MAX_PAGE_TURNS,
    ClaudeAdapter,
    HistoryState,
    Source,
    TranscriptPage,
    TranscriptTurn,
    Window,
    is_message,
    parse_claude_line,
    read_page,
    read_since,
)

# Few phones watch few agents; past this the least recently viewed file is forgotten.
MAX_TAIL_FILES: Final = 6
# Turns held per file, newest kept; a reader who pages further back is read from disk again.
MAX_HELD_TURNS: Final = 1500


@dataclass(slots=True)
class Held:
    """One file's turns from byte `cursor` to byte `end`, oldest first."""

    ident: tuple[int, int]
    end: int
    cursor: int
    turns: list[TranscriptTurn] = field(default_factory=list[TranscriptTurn])
    # The bytes just before `end`: if they changed, the file was rewritten, not appended to.
    seal: bytes = b""


@dataclass(frozen=True, slots=True)
class Ask:
    """Where a window starts: at frontier `start`, a page of messages before `before`, or the end."""

    messages: int
    start: int | None = None
    before: int | None = None
    max_turns: int = MAX_PAGE_TURNS


_SEAL_BYTES: Final = 64


def _seal(path: Path, end: int) -> bytes:
    """The last bytes before *end*, which an append never touches."""
    start = max(0, end - _SEAL_BYTES)
    with path.open("rb") as handle:
        handle.seek(start)
        return handle.read(end - start)


def _same(path: Path, held: Held, ident: tuple[int, int], size: int) -> bool:
    """Whether *path* is still the file *held* was read from, only ever appended to."""
    return held.ident == ident and size >= held.end and _seal(path, held.end) == held.seal


def _start_of_last(turns: list[TranscriptTurn], messages: int, floor: int) -> tuple[int, bool]:
    """The frontier where the newest *messages* messages begin, and whether they were all held."""
    said = 0
    for turn in reversed(turns):
        said += is_message(turn)
        if said >= messages:
            return turn.at, True
    return floor, False


class TailCache:
    """Per transcript file, the turns a page is showing, read once and then only appended to."""

    def __init__(self, *, files: int = MAX_TAIL_FILES, held: int = MAX_HELD_TURNS) -> None:
        self._files: OrderedDict[str, Held] = OrderedDict()
        self._max_files = files
        self._max_held = held
        self._lock = threading.Lock()
        # Bytes this cache has read from transcripts, for the measurements and the tests.
        self.bytes_read = 0

    def window(self, path: Path, ask: Ask) -> TranscriptPage:
        """The turns from the asked frontier to the end of *path*, oldest first."""
        with self._lock:
            try:
                held = self._fresh(path, ask)
                start = self._start(path, held, ask)
                if start < held.cursor:
                    self._extend(path, held, start)
            except OSError:
                # Found and then unreadable is not "there is none"; the view must not look quiet.
                self._files.pop(str(path), None)
                return TranscriptPage(state=HistoryState.unreadable, log=path.stem)
            start = max(start, held.cursor)
            turns = [t for t in held.turns if t.at >= start]
        return TranscriptPage(
            turns=turns,
            cursor=start,
            size=held.end,
            has_more=start > 0,
            end=held.end,
            state=HistoryState.ok if turns else HistoryState.empty,
            log=path.stem,
        )

    def _fresh(self, path: Path, ask: Ask) -> Held:
        """The held turns for *path*, built or grown so they reach its last whole line."""
        stat = path.stat()
        ident = (stat.st_dev, stat.st_ino)
        key = str(path)
        held = self._files.get(key)
        if held is None or not _same(path, held, ident, stat.st_size):
            held = self._build(path, ident, ask)
            self._files[key] = held
        elif stat.st_size > held.end:
            self._append(path, held)
        self._files.move_to_end(key)
        while len(self._files) > self._max_files:
            self._files.popitem(last=False)
        return held

    def _build(self, path: Path, ident: tuple[int, int], ask: Ask) -> Held:
        page = read_page(
            path, Window(limit=ask.max_turns, messages=ask.messages, max_bytes=MAX_HISTORY_BYTES)
        )
        self.bytes_read += page.end - page.cursor
        held = Held(ident, end=page.end, cursor=page.cursor, turns=list(page.turns))
        held.seal = _seal(path, held.end)
        return held

    def _append(self, path: Path, held: Held) -> None:
        """Read only what was written past the held end."""
        page = read_since(
            path, Window(limit=self._max_held, after=held.end, max_bytes=MAX_HISTORY_BYTES)
        )
        self.bytes_read += page.end - held.end
        if page.reset:
            raise FileNotFoundError(path)
        held.turns.extend(page.turns)
        held.end = page.end
        held.seal = _seal(path, held.end)
        self._trim(held)

    def _trim(self, held: Held) -> None:
        # Twice the hold, so a page grown by "Load earlier" is not cut on its next append.
        extra = len(held.turns) - 2 * self._max_held
        if extra > 0:
            del held.turns[:extra]
            held.cursor = held.turns[0].at

    def _start(self, path: Path, held: Held, ask: Ask) -> int:
        """The frontier the asked window begins at."""
        if ask.before is not None:
            return self._page_before(path, held, ask)
        if ask.start is not None:
            return max(0, min(ask.start, held.end))
        return _start_of_last(held.turns, ask.messages, held.cursor)[0]

    def _page_before(self, path: Path, held: Held, ask: Ask) -> int:
        """Where the page of messages that ends at `ask.before` begins."""
        before = max(0, min(ask.before or 0, held.end))
        inside = [t for t in held.turns if t.at < before]
        start, whole = _start_of_last(inside, ask.messages, held.cursor)
        if whole or held.cursor == 0:
            return start
        # Short by the messages that lie before what is held: read just those, newest first.
        missing = ask.messages - sum(map(is_message, inside))
        page = read_page(
            path,
            Window(
                limit=ask.max_turns,
                before=held.cursor,
                messages=missing,
                max_bytes=MAX_HISTORY_BYTES,
            ),
        )
        self.bytes_read += held.cursor - page.cursor
        held.turns[:0] = page.turns
        held.cursor = page.cursor
        return page.cursor

    def _extend(self, path: Path, held: Held, start: int) -> None:
        """Read the stretch from *start* up to what is held, and keep it in front."""
        first = max(start, held.cursor - MAX_HISTORY_BYTES)
        with path.open("rb") as handle:
            handle.seek(first)
            window = handle.read(held.cursor - first)
        self.bytes_read += len(window)
        found: list[TranscriptTurn] = []
        offset = first
        for raw in window.split(b"\n")[:-1]:
            turn = parse_claude_line(raw) if raw.strip() else None
            if turn is not None:
                turn.at = offset
                found.append(turn)
            offset += len(raw) + 1
        room = max(0, 2 * self._max_held - len(held.turns))
        kept = found[-room:] if room else []
        held.turns[:0] = kept
        held.cursor = first if len(kept) == len(found) else kept[0].at if kept else held.cursor


def _uncached(source: Source, ask: Ask) -> TranscriptPage:
    """A window read afresh, for a harness whose frontier is not a byte offset to append at."""
    start = ask.start
    if ask.before is not None:
        asked = Window(limit=ask.max_turns, before=ask.before, messages=ask.messages)
        start = transcript.history(source, asked).cursor
    if start is None:
        return transcript.history(source, Window(limit=ask.max_turns, messages=ask.messages))
    page = transcript.history(source, Window(limit=MAX_HELD_TURNS, after=start))
    return page.model_copy(update={"cursor": start, "has_more": start > 0})


def chat_window(source: Source, *, ask: Ask, tails: TailCache) -> TranscriptPage:
    """What the Chat tab shows: the asked window of *source*'s conversation. Never raises."""
    if source.kind != "claude":
        return _uncached(source, ask)
    path = ClaudeAdapter.log(source)
    if path is None:
        return TranscriptPage(state=HistoryState.absent)
    moved = bool(source.expect) and source.expect != path.stem
    if moved:
        # A frontier is a promise about one file; in another it would splice two threads.
        ask = Ask(messages=ask.messages, max_turns=ask.max_turns)
    page = tails.window(path, ask)
    turns = transcript.fold_tool_results(page.turns)
    return page.model_copy(update={"turns": turns, "reset": moved, "moved": moved})
