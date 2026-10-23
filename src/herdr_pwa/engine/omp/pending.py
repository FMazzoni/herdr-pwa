"""omp's pending Ask call: the newest ask in the session log's tail that no result answers."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Final

from herdr_pwa.engine.omp.ask import AskQuestion, OmpAsk
from herdr_pwa.engine.omp.records import KIND, Block, Message, blocks_of, parse_record, session_log
from herdr_pwa.engine.pages import lines_between
from herdr_pwa.engine.transcript import Source

# The tail of a session log the pending ask reader scans: the call and its result sit at the end.
_ASK_BYTES: Final = 256 * 1024


def pending_ask(source: Source) -> OmpAsk | None:
    """The last ask call with no answer, from the session file's cached tail."""
    return _pending(_located(source))


def ask_stamp(source: Source) -> str:
    """What moves when omp writes an ask or its result: its log's size and mtime, else ``""``."""
    path = _located(source)
    try:
        stat = None if path is None else path.stat()
    except OSError:
        stat = None
    return "" if stat is None else f"{stat.st_size}:{stat.st_mtime_ns}"


def _located(source: Source) -> Path | None:
    """An omp session's log, remembered per session while the file stays where it was found."""
    if source.kind != KIND or not source.session:
        return None
    roots = tuple(source.roots)
    try:
        found = _remembered_log(source.session, roots)
    except LookupError:
        return None
    if found.is_file():
        return found
    # A log moved or deleted: forget every remembered hit rather than serve a dead path.
    _remembered_log.cache_clear()
    try:
        return _remembered_log(source.session, roots)
    except LookupError:
        return None


@lru_cache(maxsize=64)
def _remembered_log(session: str, roots: tuple[Path, ...]) -> Path:
    """*session*'s log under *roots*; raising on a miss, which lru_cache never remembers."""
    found = session_log(Source(session, roots))
    if found is None:
        raise LookupError(session)
    return found


def _pending(path: Path | None) -> OmpAsk | None:
    """The unanswered ask in *path*'s tail, read once per size and mtime."""
    if path is None:
        return None
    try:
        stat = path.stat()
        return _pending_in(path, stat.st_size, stat.st_mtime_ns)
    except OSError:
        return None


@lru_cache(maxsize=32)
def _pending_in(path: Path, size: int, _mtime_ns: int) -> OmpAsk | None:
    """The newest ask call in the log's last _ASK_BYTES that no result names."""
    lines = lines_between(path, max(0, size - _ASK_BYTES), size).items
    answered: set[str] = set()
    calls: list[tuple[str, tuple[AskQuestion, ...]]] = []
    for _, line in lines:
        record = parse_record(line)
        if record is not None and record.type == "message" and record.message is not None:
            _fold_ask(record.message, answered, calls)
    # Reversed: the newest call that no result names is the one the dialog can still be showing.
    for call_id, questions in reversed(calls):
        if call_id not in answered:
            return OmpAsk(tool_call_id=call_id, questions=questions)
    return None


def _fold_ask(
    message: Message, answered: set[str], calls: list[tuple[str, tuple[AskQuestion, ...]]]
) -> None:
    """Fold one message's result id, or its ask call and questions, into the scan."""
    if message.role == "toolResult" and message.tool_call_id:
        answered.add(message.tool_call_id.strip())
    if message.role != "assistant":
        return
    for block in blocks_of(message):
        _fold_block(block, calls)


def _fold_block(block: Block, calls: list[tuple[str, tuple[AskQuestion, ...]]]) -> None:
    """Fold one content block into *calls* when it is an ask call with arguments."""
    if block.type != "toolCall" or block.name != "ask":
        return
    if block.id and block.asked is not None:
        calls.append((block.id.strip(), block.asked.questions))
