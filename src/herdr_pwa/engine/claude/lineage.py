"""Which file a Claude Code session's conversation is in now, its forks and copies followed."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

from herdr_pwa.engine.claude.records import ClaudeRecord, record_of
from herdr_pwa.engine.model import SessionId
from herdr_pwa.engine.pages import contained
from herdr_pwa.engine.transcript import TranscriptRoots, search_roots, session_name

CLAUDE_TRANSCRIPT_ROOT: Final = "~/.claude/projects"

_HEAD_BYTES: Final = 64 * 1024

# Measured: the largest compact summary on this box is 28 KB, the record a cut stamp rides.
_HEAD_FINISH: Final = 256 * 1024

_FINISH_STEP: Final = 16 * 1024

_TAIL_BYTES: Final = 64 * 1024

MAX_HOPS: Final = 8


def head_window(path: Path) -> bytes:
    """The first ``_HEAD_BYTES`` of *path*, the record they cut finished, or empty if unreadable."""
    try:
        with path.open("rb") as handle:
            window = handle.read(_HEAD_BYTES)
            if len(window) < _HEAD_BYTES or window.endswith(b"\n"):
                return window
            return window + _rest_of_record(handle)
    except OSError:
        return b""


def _rest_of_record(handle: BinaryIO) -> bytes:
    """The bytes through the next newline, or none if it is further than ``_HEAD_FINISH``."""
    # Measured on 2.1.283: a handoff's parent stamp rides a compact summary the window cuts.
    taken = bytearray()
    while len(taken) < _HEAD_FINISH:
        step = handle.read(min(_FINISH_STEP, _HEAD_FINISH - len(taken)))
        end = step.find(b"\n")
        if end >= 0:
            return bytes(taken + step[: end + 1])
        if not step:
            return b""
        taken += step
    return b""


def head_records(window: bytes) -> Iterator[ClaudeRecord]:
    """Every record *window* holds whole, in order."""
    # The trailing fragment is half a record past the budget, or empty at EOF.
    for raw in window.split(b"\n")[:-1]:
        yield record_of(raw)


def _forked_from(record: ClaudeRecord) -> str:
    """The declared parent on *record*, as logs up to 2.1.214 spell it."""
    forked = record.forked_from
    return (forked.session_id or "") if forked is not None else ""


def _stamped_parent(window: bytes, own: str) -> str:
    """The first foreign ``session_id`` in *window* -- a BACKGROUND session's stamp."""
    # The byte substrings are a cheap prefilter; `sessionKind` is confirmed on a parsed record.
    if b'"session_id"' not in window or b'"bg"' not in window:
        return ""
    stamp = ""
    for record in head_records(window):
        value = record.stamp
        if not stamp and value and value != own:
            stamp = value
        if record.session_kind == "bg":
            return stamp or _first_stamp(window, own)
    return ""


def _first_stamp(window: bytes, own: str) -> str:
    """The first foreign ``session_id`` in *window*, once the kind is confirmed."""
    # Reached when the confirming record came before any stamp, so the first pass carried none.
    for record in head_records(window):
        value = record.stamp
        if value and value != own:
            return value
    return ""


def copied_from(path: Path) -> str:
    """The session *path*'s thread was COPIED OUT OF, per its own head window."""
    window = head_window(path)
    head = next((r for r in head_records(window) if r.session is not None), ClaudeRecord())
    return _forked_from(head) or _stamped_parent(window, head.session or "")


def _is_compact(record: ClaudeRecord) -> bool:
    """Whether *record* is a compaction boundary, however the CLI spelled it."""
    return (
        record.compact_metadata is not None
        or record.is_compact_summary is True
        or record.subtype == "compact_boundary"
    )


def _background_handoff(path: Path) -> bool:
    """Whether *path*'s head declares a compaction AND a background session."""
    window = head_window(path)
    if b'"bg"' not in window:
        return False
    background = False
    compacted = False
    for record in head_records(window):
        background = background or record.session_kind == "bg"
        compacted = compacted or _is_compact(record)
        if background and compacted:
            return True
    return False


def _tail_window(path: Path) -> bytes:
    """The last ``_TAIL_BYTES`` of *path*, or empty if it cannot be read."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - _TAIL_BYTES))
            return handle.read(_TAIL_BYTES)
    except OSError:
        return b""


def _last_timestamp(path: Path) -> str:
    """The newest ``timestamp`` in *path*, from a bounded tail read."""
    for raw in reversed(_tail_window(path).split(b"\n")):
        stamp = record_of(raw).timestamp
        if stamp:
            return stamp
    return ""


def _last_writer(path: Path) -> str:
    """The session whose stamp is on *path*'s newest stamped record, from a bounded tail read."""
    for raw in reversed(_tail_window(path).split(b"\n")):
        stamp = record_of(raw).stamp
        if stamp:
            return stamp
    return ""


# A head never changes once the file outgrows the window and its finish, so it is read once.
_heads: Final[dict[tuple[str, int, int, int], str]] = {}

_HEADS_MAX: Final = 4096


def _copied_from_cached(path: Path) -> str:
    """`copied_from`, remembered per file for as long as its head window cannot change."""
    try:
        stat = path.stat()
    except OSError:
        return ""
    key = (str(path), stat.st_dev, stat.st_ino, min(stat.st_size, _HEAD_BYTES + _HEAD_FINISH))
    found = _heads.get(key)
    if found is None:
        if len(_heads) >= _HEADS_MAX:
            _heads.clear()
        found = _heads[key] = copied_from(path)
    return found


def _copies(directory: Path) -> dict[str, list[Path]]:
    """Parent session -> the sibling logs that declare themselves copied from it."""
    edges: dict[str, list[Path]] = {}
    try:
        siblings = sorted(directory.glob("*.jsonl"))
    except OSError:
        return edges
    for sibling in siblings:
        parent = _copied_from_cached(sibling)
        if parent:
            edges.setdefault(parent, []).append(sibling)
    return edges


# One step of the walk: a log's session name, and its path once contained to the root.
_Hop = tuple[str, Path]


@dataclass(slots=True, frozen=True)
class _Lineage:
    """What every hop of one walk shares: where it may go, who it is for, what it has seen."""

    base: Path
    session: str
    edges: dict[str, list[Path]]
    seen: set[str]


def _descendants(named: Path, lineage: _Lineage) -> list[Path]:
    """Every log downstream of *named* that may replace its parent, breadth-first and bounded."""
    lineage.seen.add(lineage.session)
    frontier: list[_Hop] = [(lineage.session, named)]
    found: list[Path] = []
    for _ in range(MAX_HOPS):
        frontier = _one_hop(frontier, lineage)
        if not frontier:
            return found
        found.extend(path for _, path in frontier)
    return found


def _one_hop(frontier: list[_Hop], lineage: _Lineage) -> list[_Hop]:
    """The unseen children of *frontier* that each hold no less than the log they came from."""
    onward: list[_Hop] = []
    for parent, path in frontier:
        children = [
            child for child in lineage.edges.get(parent, ()) if child.stem not in lineage.seen
        ]
        lineage.seen.update(child.stem for child in children)
        # Per edge, not off the named file: every hop after a compacted handoff is smaller than it.
        floor = _size(path)
        if floor is not None:
            onward += _admitted(children, lineage, floor)
    return onward


def _admitted(children: list[Path], lineage: _Lineage, floor: int) -> list[_Hop]:
    """The *children* that may replace a log of *floor* bytes, each contained to the root."""
    found: list[_Hop] = []
    for child in children:
        resolved = _eligible(child, lineage, floor)
        if resolved is not None:
            found.append((child.stem, resolved))
    return found


def _size(path: Path) -> int | None:
    """*path*'s size in bytes, or ``None`` when it cannot be read."""
    try:
        return path.stat().st_size
    except OSError:
        return None


def _follow(named: Path, session: str, base: Path) -> Path:
    """*named*, or the copy the conversation continued in. Never LESS history."""
    newest = _last_timestamp(named)
    if not newest:
        return named
    best = named
    lineage = _Lineage(base, session, _copies(named.parent), set())
    for candidate in _descendants(named, lineage):
        stamp = _last_timestamp(candidate)
        if stamp > newest:
            best, newest = candidate, stamp
    return best


def _eligible(candidate: Path, lineage: _Lineage, floor: int) -> Path | None:
    """*candidate* resolved, if it may replace a log of *floor* bytes, else ``None``."""
    # Contained to the same root the named file came through, never a union of roots.
    resolved = contained(candidate, lineage.base)
    if resolved is None:
        return None
    try:
        smaller = resolved.stat().st_size < floor
    except OSError:
        return None
    if smaller and not _waived(resolved, lineage.session):
        return None
    return resolved


def _waived(path: Path, session: str) -> bool:
    """Whether *path* may hold less than its parent: a compacted handoff, or *session* writing it."""
    # Measured on 2.1.289: a compaction can move the session into a smaller foreground copy.
    return _background_handoff(path) or _last_writer(path) == session


def in_root(base: Path, session: str, *, follow: bool) -> Path | None:
    """*session*'s log inside ONE root, contained to THAT root and no other."""
    try:
        base = base.resolve()
        matches = sorted(base.glob(f"*/{session}.jsonl"))
    except OSError:
        return None
    for path in matches:
        resolved = contained(path, base)
        if resolved is None:
            continue
        return _follow(resolved, session, base) if follow else resolved
    return None


def locate(
    session: SessionId | str,
    *,
    root: TranscriptRoots = None,
) -> Path | None:
    """The file *session*'s conversation is actually in now, or ``None``."""
    text = session_name(session)
    if not text:
        return None
    for base in search_roots(root, kind="claude", default=(CLAUDE_TRANSCRIPT_ROOT,)):
        # A root that cannot be read must not blank the roots after it.
        found = in_root(base, text, follow=True)
        if found is not None:
            return found
    return None
