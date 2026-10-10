"""The provider transcript: the only source that is a conversation, not a screen."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Iterator, Sequence
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Final, NamedTuple, Protocol

from pydantic import BaseModel, Field, JsonValue

from herdr_pwa.diff import FileDiff, tool_diff
from herdr_pwa.engine.model import SessionId
from herdr_pwa.jsontypes import JsonObject, as_object, loads, objects

CLAUDE_TRANSCRIPT_ROOT: Final = "~/.claude/projects"

AGENT_KINDS: Final = ("claude", "opencode", "hermes")

# A path, or the string an env var carries.
type OneRoot = str | os.PathLike[str]

# One root, several searched in order, or the provider's default.
type TranscriptRoots = OneRoot | Sequence[OneRoot] | None

type TurnParser = Callable[[JsonObject], TranscriptTurn | None]

MAX_PAGE_BYTES: Final = 4 * 1024 * 1024

MAX_PROSE_CHARS: Final = 12000

MAX_RESULT_CHARS: Final = 2000

MAX_NOTE_CHARS: Final = 2000

MAX_SUMMARY_CHARS: Final = 160

MAX_USAGE_BYTES: Final = 64 * 1024 * 1024

_CHUNK: Final = 256 * 1024

_HEAD_BYTES: Final = 64 * 1024

_TAIL_BYTES: Final = 64 * 1024

_ANOTHER_FILE: Final = 1 << 62

_MAX_HOPS: Final = 8


class HistoryState(StrEnum):
    """Why a page has the turns it has -- or has none."""

    ok = "ok"
    empty = "empty"
    absent = "absent"
    unreadable = "unreadable"
    unsupported = "unsupported"


class Speaker(StrEnum):
    """Who a turn is attributed to in the chat."""

    human = "human"

    agent = "agent"

    tool = "tool"

    system = "system"


class TranscriptTurn(BaseModel):
    """One renderable line of conversation."""

    uuid: str
    speaker: Speaker
    text: str
    ts: str = ""
    tool: str = ""
    truncated: bool = False
    # The tool_use id, carried by a call and by its result: the only thing pairing the two.
    call: str = ""
    result: str = ""
    failed: bool = False
    result_truncated: bool = False
    # Kept out of `text`, which stays the one-line gist (the path), as Collie's summarizeToolInput does.
    diff: FileDiff | None = None


class TranscriptPage(BaseModel):
    """One page of turns, oldest first, plus what to ask for next."""

    turns: list[TranscriptTurn] = Field(default_factory=list)
    cursor: int = 0
    size: int = 0
    has_more: bool = False
    # Handed back as `after`, it reads only what was appended, so the chat appends.
    end: int = 0
    # The file shrank under the held cursor: re-read the tail rather than splice.
    reset: bool = False
    # The reset was a move to another file, not a rewrite; the view words them differently.
    moved: bool = False
    # Absent is the only thing true of a page nobody read.
    state: HistoryState = HistoryState.absent
    # The file actually read, which is not the named session once a fork was followed.
    log: str = ""


# Locating the file


def _for_kind(entry: str, kind: str) -> str:
    """*entry* as it applies to *kind*: unqualified, qualified for it, or empty."""
    head, sep, rest = entry.partition(":")
    if not sep or head.strip().lower() not in AGENT_KINDS:
        return entry
    return rest.strip() if head.strip().lower() == kind else ""


def _roots(
    root: TranscriptRoots = None,
    *,
    kind: str = "claude",
    default: Sequence[str] = (CLAUDE_TRANSCRIPT_ROOT,),
) -> list[Path]:
    """Every configured transcript root for *kind*, in search order, deduplicated."""
    if root is None:
        given: Sequence[OneRoot] = list(default)
    elif isinstance(root, (str, os.PathLike)):
        given = [root]
    else:
        given = list(root)
    found: list[Path] = []
    for entry in given:
        # An empty entry would resolve against the process cwd, nobody's transcript directory.
        text = _for_kind(str(entry).strip(), kind)
        if not text.strip():
            continue
        path = Path(text.strip()).expanduser()
        if path not in found:
            found.append(path)
    return found


def _contained(path: Path, base: Path) -> Path | None:
    """*path* resolved, then re-checked to still be a file inside *base*."""
    # A symlink satisfies any comparison of the strings we were handed, so resolve first.
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_relative_to(base) or not resolved.is_file():
        return None
    return resolved


def _head_window(path: Path) -> bytes:
    """The first ``_HEAD_BYTES`` of *path*, or empty if it cannot be read."""
    try:
        with path.open("rb") as handle:
            return handle.read(_HEAD_BYTES)
    except OSError:
        return b""


def _head_records(window: bytes) -> Iterator[JsonObject]:
    """Every record *window* holds whole, in order."""
    # The trailing fragment is half a record past the budget, or empty at EOF.
    for raw in window.split(b"\n")[:-1]:
        yield _loads(raw)


def _loads(raw: bytes) -> JsonObject:
    """One JSONL line as a dict, or an empty one for anything that is not."""
    if not raw.strip():
        return {}
    try:
        record = loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    found = as_object(record)
    return found if found is not None else {}


def _forked_from(record: JsonObject) -> str:
    """The declared parent on *record*, as logs up to 2.1.214 spell it."""
    forked = as_object(record.get("forkedFrom"))
    parent = forked.get("sessionId") if forked is not None else None
    return parent if isinstance(parent, str) else ""


def _stamped_parent(window: bytes, own: str) -> str:
    """The first foreign ``session_id`` in *window* -- a BACKGROUND session's stamp."""
    # The byte substrings are a cheap prefilter; `sessionKind` is confirmed on a parsed record.
    if b'"session_id"' not in window or b'"bg"' not in window:
        return ""
    stamp = ""
    for record in _head_records(window):
        value = record.get("session_id")
        if not stamp and isinstance(value, str) and value and value != own:
            stamp = value
        if record.get("sessionKind") == "bg":
            return stamp or _first_stamp(window, own)
    return ""


def _first_stamp(window: bytes, own: str) -> str:
    """The first foreign ``session_id`` in *window*, once the kind is confirmed."""
    # Reached when the confirming record came before any stamp, so the first pass carried none.
    for record in _head_records(window):
        value = record.get("session_id")
        if isinstance(value, str) and value and value != own:
            return value
    return ""


def _copied_from(path: Path) -> str:
    """The session *path*'s thread was COPIED OUT OF, per its own head window."""
    window = _head_window(path)
    head: JsonObject = {}
    for record in _head_records(window):
        if isinstance(record.get("sessionId"), str):
            head = record
            break
    return _forked_from(head) or _stamped_parent(window, str(head.get("sessionId", "")))


def _is_compact(record: JsonObject) -> bool:
    """Whether *record* is a compaction boundary, however the CLI spelled it."""
    return (
        isinstance(record.get("compactMetadata"), dict)
        or record.get("isCompactSummary") is True
        or record.get("subtype") == "compact_boundary"
    )


def _background_handoff(path: Path) -> bool:
    """Whether *path*'s head declares a compaction AND a background session."""
    window = _head_window(path)
    if b'"bg"' not in window:
        return False
    background = False
    compacted = False
    for record in _head_records(window):
        background = background or record.get("sessionKind") == "bg"
        compacted = compacted or _is_compact(record)
        if background and compacted:
            return True
    return False


def _last_timestamp(path: Path) -> str:
    """The newest ``timestamp`` in *path*, from a bounded tail read."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - _TAIL_BYTES))
            window = handle.read(_TAIL_BYTES)
    except OSError:
        return ""
    for raw in reversed(window.split(b"\n")):
        stamp = _loads(raw).get("timestamp")
        if isinstance(stamp, str) and stamp:
            return stamp
    return ""


def _copies(directory: Path) -> dict[str, list[Path]]:
    """Parent session -> the sibling logs that declare themselves copied from it."""
    edges: dict[str, list[Path]] = {}
    try:
        siblings = sorted(directory.glob("*.jsonl"))
    except OSError:
        return edges
    for sibling in siblings:
        parent = _copied_from(sibling)
        if parent:
            edges.setdefault(parent, []).append(sibling)
    return edges


def _descendants(session: str, edges: dict[str, list[Path]]) -> list[Path]:
    """Every log downstream of *session*, breadth-first, bounded and cycle-safe."""
    seen = {session}
    frontier = [session]
    found: list[Path] = []
    for _ in range(_MAX_HOPS):
        onward = _one_hop(frontier, edges, seen, found)
        if not onward:
            return found
        frontier = onward
    return found


def _one_hop(
    frontier: list[str], edges: dict[str, list[Path]], seen: set[str], found: list[Path]
) -> list[str]:
    """The unseen children of *frontier*, recorded into *seen* and *found*."""
    onward: list[str] = []
    for parent in frontier:
        for child in edges.get(parent, ()):
            if child.stem in seen:
                continue
            seen.add(child.stem)
            found.append(child)
            onward.append(child.stem)
    return onward


def _follow(named: Path, session: str, base: Path) -> Path:
    """*named*, or the copy the conversation continued in. Never LESS history."""
    newest = _last_timestamp(named)
    try:
        floor = named.stat().st_size
    except OSError:
        return named
    if not newest:
        return named
    best = named
    for candidate in _descendants(session, _copies(named.parent)):
        resolved = _eligible(candidate, base, floor)
        if resolved is None:
            continue
        stamp = _last_timestamp(resolved)
        if stamp > newest:
            best, newest = resolved, stamp
    return best


def _eligible(candidate: Path, base: Path, floor: int) -> Path | None:
    """*candidate* resolved, if it may replace a log of *floor* bytes, else ``None``."""
    # Contained to the same root the named file came through, never a union of roots.
    resolved = _contained(candidate, base)
    if resolved is None:
        return None
    try:
        if resolved.stat().st_size < floor and not _background_handoff(resolved):
            return None
    except OSError:
        return None
    return resolved


def _in_root(base: Path, session: str, *, follow: bool) -> Path | None:
    """*session*'s log inside ONE root, contained to THAT root and no other."""
    try:
        base = base.resolve()
        matches = sorted(base.glob(f"*/{session}.jsonl"))
    except OSError:
        return None
    for path in matches:
        resolved = _contained(path, base)
        if resolved is None:
            continue
        return _follow(resolved, session, base) if follow else resolved
    return None


def _session_id(session: SessionId | str) -> str:
    """*session* as a NAME, or ``""`` for anything that is really a path."""
    # A session id off a URL segment must not walk the tree.
    text = str(session).strip()
    if not text or "/" in text or "\\" in text or text.startswith("."):
        return ""
    return text


def locate(
    session: SessionId | str,
    *,
    root: TranscriptRoots = None,
    follow: bool = True,
) -> Path | None:
    """The file *session*'s conversation is actually in now, or ``None``."""
    text = _session_id(session)
    if not text:
        return None
    for base in _roots(root, kind="claude"):
        # A root that cannot be read must not blank the roots after it.
        found = _in_root(base, text, follow=follow)
        if found is not None:
            return found
    return None


# Reading it, backwards, bounded


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


def _parse_line(raw: bytes, parse: TurnParser) -> TranscriptTurn | None:
    """One JSONL line as a turn, or ``None`` for anything that is not one."""
    try:
        record = as_object(loads(raw))
    except (ValueError, UnicodeDecodeError):
        return None
    return parse(record) if record is not None else None


def _take_backwards(
    lines: list[tuple[int, bytes]],
    parse: TurnParser,
    turns: list[TranscriptTurn],
    limit: int,
) -> tuple[int | None, bool]:
    """Append the turns in *lines*, newest first, up to *limit*."""
    oldest: int | None = None
    for offset, raw in reversed(lines):
        turn = _parse_line(raw, parse)
        if turn is None:
            continue
        turns.append(turn)
        oldest = offset
        if len(turns) >= limit:
            return oldest, True
    return oldest, False


def _walk_back(
    path: Path, end: int, parse: TurnParser, *, limit: int, max_bytes: int
) -> tuple[list[TranscriptTurn], int]:
    """The turns newest first from byte *end* backwards, and the page cursor."""
    turns: list[TranscriptTurn] = []
    frontier = end
    cursor = end
    spent = 0
    hit_limit = False
    while frontier > 0 and len(turns) < limit and spent < max_bytes:
        lines, start = _lines_before(path, frontier, min(_CHUNK * 4, max_bytes - spent))
        if start == frontier:  # no progress; nothing more is readable
            break
        spent += frontier - start
        frontier = start
        cursor = start
        taken, hit_limit = _take_backwards(lines, parse, turns, limit)
        cursor = start if taken is None else taken
    if frontier == 0 and not hit_limit:
        cursor = 0
    return turns, cursor


def read_page(
    path: Path,
    *,
    limit: int = 40,
    before: int | None = None,
    max_bytes: int = MAX_PAGE_BYTES,
    parse: TurnParser | None = None,
) -> TranscriptPage:
    """Up to *limit* turns ending at byte *before*, oldest first."""
    parse = parse or _claude_turn
    try:
        size = path.stat().st_size
    except OSError:
        return TranscriptPage(state=HistoryState.unreadable)
    end = _complete_end(path, size if before is None else max(0, min(before, size)))
    turns, cursor = _walk_back(path, end, parse, limit=limit, max_bytes=max_bytes)
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


def read_since(
    path: Path,
    *,
    after: int,
    limit: int = 40,
    max_bytes: int = MAX_PAGE_BYTES,
    parse: TurnParser | None = None,
) -> TranscriptPage:
    """Whatever has been appended past byte *after*, oldest first."""
    parse = parse or _claude_turn
    try:
        size = path.stat().st_size
    except OSError:
        return TranscriptPage(state=HistoryState.unreadable)
    start = max(0, after)
    if start > size:
        return TranscriptPage(
            size=size, end=size, reset=True, state=HistoryState.empty, log=path.stem
        )
    with path.open("rb") as handle:
        handle.seek(start)
        window = handle.read(min(max_bytes, size - start))
    tail = window.rfind(b"\n")
    if tail < 0:
        # No whole line yet, so the cursor must not move past bytes never delivered.
        return TranscriptPage(
            size=size,
            end=start,
            has_more=size > start,
            state=HistoryState.empty,
            log=path.stem,
        )
    turns: list[TranscriptTurn] = []
    end = _take_forward(window[:tail], start, parse, turns, limit)
    return TranscriptPage(
        turns=turns,
        cursor=start,
        size=size,
        end=end,
        has_more=end < size,
        state=HistoryState.ok if turns else HistoryState.empty,
        log=path.stem,
    )


def _take_forward(
    window: bytes, start: int, parse: TurnParser, turns: list[TranscriptTurn], limit: int
) -> int:
    """Append the turns in *window* oldest first, up to *limit*; return the end offset."""
    end = start
    offset = start
    for raw in window.split(b"\n"):
        offset += len(raw) + 1
        end = offset
        if not raw.strip():
            continue
        turn = _parse_line(raw, parse)
        if turn is not None:
            turns.append(turn)
        if len(turns) >= limit:
            break
    return end


# The Claude Code adapter

_SKIP_TYPES: Final = frozenset(
    {
        "attachment",
        "ai-title",
        "atis-latch",
        "file-history-delta",
        "file-history-snapshot",
        "frame-link",
        "last-prompt",
        "mode",
        "permission-mode",
        "queue-operation",
        "system",
    }
)

_HUMAN_SOURCES: Final = frozenset({"typed", "queued", "pasted"})


def _is_human(record: JsonObject) -> bool:
    source: JsonValue = record.get("promptSource")
    if isinstance(source, dict):
        source = source.get("kind")
    if isinstance(source, str) and source in _HUMAN_SOURCES:
        return True
    origin = as_object(record.get("origin"))
    return origin is not None and origin.get("kind") == "human"


def clip(text: str, limit: int) -> tuple[str, bool]:
    """*text* stripped and cut to *limit* characters, with whether it was cut."""
    text = text.strip()
    if len(text) <= limit:
        return text, False
    return text[:limit], True


_TOOL_ARGUMENT_KEYS: Final = (
    "command",
    "file_path",
    "notebook_path",
    "path",
    "pattern",
    "query",
    "url",
    "description",
    "summary",
    "skill",
    "subagent_type",
    "name",
    "prompt",
    "text",
)


def one_line(value: object, limit: int = MAX_SUMMARY_CHARS) -> tuple[str, bool]:
    """*value* as a single collapsed line, clipped, with whether it was clipped."""
    if not isinstance(value, str):
        return "", False
    return clip(" ".join(value.split()), limit)


def tool_summary(params: object) -> tuple[str, bool]:
    """The one identifying argument of a tool call (see `_TOOL_ARGUMENT_KEYS`)."""
    args = as_object(params)
    if args is None:
        return "", False
    for key in _TOOL_ARGUMENT_KEYS:
        if key in args:
            line, cut = one_line(args.get(key))
            if line:
                return line, cut
    return "", False


def _blocks(content: object) -> Iterator[JsonObject]:
    yield from objects(content)


def _content(record: JsonObject) -> JsonValue:
    """``record["message"]["content"]``; callers have already checked the message is an object."""
    message = as_object(record.get("message"))
    return message.get("content") if message is not None else None


def _user_record(record: JsonObject, uuid: str, ts: str) -> TranscriptTurn | None:
    """A ``role: "user"`` record, which is where the whole trap lives."""
    content = _content(record)
    if isinstance(content, str):
        if record.get("isMeta"):
            return None
        # The cap follows the speaker: injected machinery gets the note cap, as the view shows three lines.
        human = _is_human(record)
        text, cut = clip(content, MAX_PROSE_CHARS if human else MAX_NOTE_CHARS)
        if not text:
            return None
        return TranscriptTurn(
            uuid=uuid,
            speaker=Speaker.human if human else Speaker.system,
            text=text,
            ts=ts,
            truncated=cut,
        )
    meta = bool(record.get("isMeta"))
    for block in _blocks(content):
        turn = _user_block(block, uuid, ts, meta=meta)
        if turn is not None:
            return turn
    return None


def _user_block(block: JsonObject, uuid: str, ts: str, *, meta: bool) -> TranscriptTurn | None:
    """One content block of a user record, or ``None`` if it is not conversation."""
    kind = block.get("type")
    if kind == "tool_result":
        body = block.get("content")
        text, cut = clip(
            body if isinstance(body, str) else json.dumps(body, ensure_ascii=False),
            MAX_RESULT_CHARS,
        )
        return TranscriptTurn(
            uuid=uuid,
            speaker=Speaker.tool,
            text=text,
            ts=ts,
            tool="result",
            truncated=cut,
            call=str(block.get("tool_use_id") or ""),
            failed=block.get("is_error") is True,
        )
    if kind == "text" and not meta:
        text, cut = clip(str(block.get("text", "")), MAX_NOTE_CHARS)
        if text:
            return TranscriptTurn(
                uuid=uuid, speaker=Speaker.system, text=text, ts=ts, truncated=cut
            )
    return None


def _assistant_record(record: JsonObject, uuid: str, ts: str) -> TranscriptTurn | None:
    """An assistant record: prose, or one collapsed tool call."""
    for block in _blocks(_content(record)):
        btype = block.get("type")
        if btype == "text":
            text, cut = clip(str(block.get("text", "")), MAX_PROSE_CHARS)
            if not text:
                return None
            return TranscriptTurn(uuid=uuid, speaker=Speaker.agent, text=text, ts=ts, truncated=cut)
        if btype == "tool_use":
            params = block.get("input")
            text, cut = tool_summary(params)
            return TranscriptTurn(
                uuid=uuid,
                speaker=Speaker.tool,
                text=text,
                ts=ts,
                tool=str(block.get("name", "")),
                truncated=cut,
                call=str(block.get("id") or ""),
                diff=tool_diff(params),
            )
        if btype == "thinking":
            # Measured: an empty `thinking` string and a signature blob, so nothing to render.
            return None
    return None


def _claude_turn(record: JsonObject) -> TranscriptTurn | None:
    """One Claude Code JSONL record as a chat turn, or ``None`` to drop it."""
    kind = record.get("type")
    if kind in _SKIP_TYPES:
        return None
    uuid = record.get("uuid")
    if not isinstance(uuid, str) or not uuid:
        return None
    if not isinstance(record.get("message"), dict):
        return None
    stamp = record.get("timestamp")
    ts = stamp if isinstance(stamp, str) else ""
    if kind == "user":
        return _user_record(record, uuid, ts)
    if kind == "assistant":
        return _assistant_record(record, uuid, ts)
    return None


def fold_tool_results(turns: Iterable[TranscriptTurn]) -> list[TranscriptTurn]:
    """Fold each tool RESULT onto the call it answers, by ``tool_use_id``."""
    folded: list[TranscriptTurn] = []
    by_call: dict[str, int] = {}
    for turn in turns:
        if turn.speaker is Speaker.tool and turn.tool == "result" and turn.call:
            index = by_call.pop(turn.call, -1)
            if index >= 0:
                call = folded[index]
                folded[index] = call.model_copy(
                    update={
                        "result": turn.text,
                        "failed": turn.failed,
                        "result_truncated": turn.truncated,
                    }
                )
                continue
        if turn.speaker is Speaker.tool and turn.call and turn.tool != "result":
            by_call[turn.call] = len(folded)
        folded.append(turn)
    return folded


class TranscriptAdapter(Protocol):
    """What a harness must answer for its conversation to reach the Chat tab."""

    default_roots: ClassVar[tuple[str, ...]]

    def page(
        self,
        session: str,
        *,
        roots: Sequence[Path],
        limit: int = 40,
        before: int | None = None,
        expect: str = "",
    ) -> TranscriptPage:
        """Up to *limit* turns ending at frontier *before*, oldest first."""
        ...

    def since(
        self,
        session: str,
        *,
        roots: Sequence[Path],
        after: int,
        limit: int = 40,
        expect: str = "",
    ) -> TranscriptPage:
        """Whatever has been recorded past frontier *after*, oldest first."""
        ...

    def tally(
        self, session: str, *, roots: Sequence[Path], max_bytes: int = MAX_USAGE_BYTES
    ) -> SessionUsage:
        """What *session* has spent, over everything its thread has lived in."""
        ...


class ClaudeAdapter:
    """Claude Code: one JSONL per session, and the frontier is a BYTE OFFSET."""

    default_roots: ClassVar[tuple[str, ...]] = (CLAUDE_TRANSCRIPT_ROOT,)

    def page(
        self,
        session: str,
        *,
        roots: Sequence[Path],
        limit: int = 40,
        before: int | None = None,
        expect: str = "",
    ) -> TranscriptPage:
        """Up to *limit* turns ending at byte *before*, oldest first."""
        path = self._log(session, roots, follow=True)
        if path is None:
            return TranscriptPage(state=HistoryState.absent)
        if expect and expect != path.stem:
            # A `before` offset into another file would be clamped, not refused, so drop it.
            page = read_page(path, limit=limit, before=None, parse=_claude_turn)
            return page.model_copy(update={"reset": True, "moved": True})
        return read_page(path, limit=limit, before=before, parse=_claude_turn)

    def since(
        self,
        session: str,
        *,
        roots: Sequence[Path],
        after: int,
        limit: int = 40,
        expect: str = "",
    ) -> TranscriptPage:
        """Whatever has been APPENDED past byte *after*, oldest first."""
        path = self._log(session, roots, follow=True)
        if path is None:
            return TranscriptPage(state=HistoryState.absent)
        if expect and expect != path.stem:
            # A held cursor is a promise about one file; reading it here would splice two threads.
            page = read_since(path, after=_ANOTHER_FILE, limit=limit)
            return page.model_copy(update={"moved": True})
        return read_since(path, after=after, limit=limit, parse=_claude_turn)

    def tally(
        self, session: str, *, roots: Sequence[Path], max_bytes: int = MAX_USAGE_BYTES
    ) -> SessionUsage:
        """What *session* has spent, summed over its whole lineage."""
        path = self._log(session, roots, follow=True)
        if path is None:
            return SessionUsage(state=HistoryState.absent)
        return _tally_lineage(path, max_bytes=max_bytes)

    @staticmethod
    def _log(session: str, roots: Sequence[Path], *, follow: bool) -> Path | None:
        """*session*'s log in the first root holding it, contained to THAT root."""
        for base in roots:
            found = _in_root(base, session, follow=follow)
            if found is not None:
                return found
        return None


_ADAPTERS: Final[dict[str, TranscriptAdapter]] = {}


def _registry() -> dict[str, TranscriptAdapter]:
    """The adapters, built on first use."""
    if not _ADAPTERS:
        # The adapters import this module, so importing them at the top would be a cycle.
        from herdr_pwa.engine.hermes import HermesAdapter  # noqa: PLC0415
        from herdr_pwa.engine.opencode import OpencodeAdapter  # noqa: PLC0415

        _ADAPTERS["claude"] = ClaudeAdapter()
        _ADAPTERS["opencode"] = OpencodeAdapter()
        _ADAPTERS["hermes"] = HermesAdapter()
    return _ADAPTERS


def adapter_for(agent_kind: str | None) -> TranscriptAdapter | None:
    """The adapter for *agent_kind*, or ``None`` if there is not one."""
    return _registry().get((agent_kind or "").strip().lower())


def history(  # noqa: PLR0913
    session: SessionId | str,
    *,
    agent_kind: str | None = "claude",
    limit: int = 40,
    before: int | None = None,
    after: int | None = None,
    log: str = "",
    root: TranscriptRoots = None,
) -> TranscriptPage:
    """One page of *session*'s conversation, oldest first. Never raises."""
    kind = (agent_kind or "").strip().lower()
    adapter = adapter_for(kind)
    text = _session_id(session)
    if adapter is None:
        return TranscriptPage(state=HistoryState.unsupported)
    if not text:
        return TranscriptPage(state=HistoryState.absent)
    roots = _roots(root, kind=kind, default=adapter.default_roots)
    try:
        page = (
            adapter.since(text, roots=roots, after=after, limit=limit, expect=log)
            if after is not None
            else adapter.page(text, roots=roots, limit=limit, before=before, expect=log)
        )
    except OSError:
        # Found and then unreadable is not "there is none"; the view must not show a quiet chat.
        return TranscriptPage(state=HistoryState.unreadable)
    return page.model_copy(update={"turns": fold_tool_results(page.turns)})


# What it cost: the usage tally


class ModelRef(NamedTuple):
    """One model that answered, and the provider that served it."""

    model: str = ""
    provider: str = ""


class SessionUsage(BaseModel):
    """What one session has spent, summed over every log its thread has lived in."""

    # Distinct assistant messages, not records.
    messages: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cache_creation_tokens: int = 0
    # Measured at 200 000x input on a real session: the figure a naive sum leaves out.
    cache_read_tokens: int = 0
    # The newest message's whole prompt: how full the window is now, not a total.
    context_tokens: int = 0
    models: list[ModelRef] = Field(default_factory=list)
    # Only where the provider records it; a price table kept here would be worse than a gap. Zero is unknown.
    cost: float = 0.0
    logs: int = 0
    # A byte budget stopped the read, so the sums are floors.
    partial: bool = False
    state: HistoryState = HistoryState.absent


_SYNTHETIC_MODEL: Final = "<synthetic>"


def _usage_of(record: JsonObject) -> tuple[str, JsonObject, str] | None:
    """An assistant record's ``(message id, usage, model)``, or ``None``."""
    if record.get("type") != "assistant":
        return None
    message = as_object(record.get("message"))
    if message is None:
        return None
    usage = as_object(message.get("usage"))
    identifier = message.get("id")
    if usage is None or not isinstance(identifier, str) or not identifier:
        return None
    named = message.get("model")
    model = named if isinstance(named, str) else ""
    if model == _SYNTHETIC_MODEL:
        return None
    return identifier, usage, model


def _count(usage: JsonObject, key: str) -> int:
    """One usage field as a non-negative int, whatever the provider actually wrote."""
    value = usage.get(key)
    return value if isinstance(value, int) and value > 0 else 0


def _add_usage(tally: SessionUsage, usage: JsonObject, model: str) -> None:
    """Fold one message's usage into *tally*, in place."""
    details = as_object(usage.get("output_tokens_details"))
    tally.messages += 1
    tally.input_tokens += _count(usage, "input_tokens")
    tally.output_tokens += _count(usage, "output_tokens")
    tally.cache_creation_tokens += _count(usage, "cache_creation_input_tokens")
    tally.cache_read_tokens += _count(usage, "cache_read_input_tokens")
    if details is not None:
        tally.thinking_tokens += _count(details, "thinking_tokens")
    if model:
        # Claude Code answers from one provider, so the reference carries none.
        remember(tally, ModelRef(model))


def remember(tally: SessionUsage, ref: ModelRef) -> None:
    """Record *ref* as having answered, once, keeping first-seen order."""
    if ref.model and ref not in tally.models:
        tally.models.append(ref)


def _prompt_size(usage: JsonObject) -> int:
    """Everything that went UP with one message: fresh, cache-written, cache-read."""
    return (
        _count(usage, "input_tokens")
        + _count(usage, "cache_creation_input_tokens")
        + _count(usage, "cache_read_input_tokens")
    )


def _tally_log(
    path: Path, tally: SessionUsage, seen: set[str], budget: int, *, newest: bool
) -> int:
    """Add *path*'s messages to *tally*; return the bytes it did not spend."""
    with path.open("rb") as handle:
        for raw in handle:
            budget -= len(raw)
            if budget < 0:
                tally.partial = True
                return 0
            if b'"usage"' in raw:
                _tally_line(raw, tally, seen, newest=newest)
    return budget


def _tally_line(raw: bytes, tally: SessionUsage, seen: set[str], *, newest: bool) -> None:
    """Add one record's message to *tally*, unless its id was already counted."""
    found = _usage_of(_loads(raw))
    if found is None:
        return
    identifier, usage, model = found
    if newest:
        # Read forward, so the last message to land here is the live window.
        tally.context_tokens = _prompt_size(usage)
    # One message is several records carrying identical usage; summing records nearly doubles it.
    if identifier in seen:
        return
    seen.add(identifier)
    _add_usage(tally, usage, model)


def _ancestors(path: Path) -> list[Path]:
    """Every log *path*'s thread was copied out of, newest first."""
    directory = path.parent
    chain: list[Path] = []
    current = path
    for _ in range(_MAX_HOPS):
        parent = _copied_from(current)
        if not parent:
            return chain
        found = _contained(directory / f"{parent}.jsonl", directory)
        if found is None or found == path or found in chain:
            return chain
        chain.append(found)
        current = found
    return chain


def _tally_lineage(path: Path, *, max_bytes: int) -> SessionUsage:
    """Sum *path* and every log its thread was copied out of."""
    tally = SessionUsage()
    seen: set[str] = set()
    budget = max_bytes
    for index, log in enumerate([path, *_ancestors(path)]):
        tally.logs += 1
        budget = _tally_log(log, tally, seen, budget, newest=index == 0)
        if tally.partial:
            break
    tally.state = HistoryState.ok if tally.messages else HistoryState.empty
    return tally


def usage(
    session: SessionId | str,
    *,
    agent_kind: str | None = "claude",
    root: TranscriptRoots = None,
    max_bytes: int = MAX_USAGE_BYTES,
) -> SessionUsage:
    """What *session* has spent so far, across its whole lineage. Never raises."""
    kind = (agent_kind or "").strip().lower()
    adapter = adapter_for(kind)
    text = _session_id(session)
    if adapter is None:
        return SessionUsage(state=HistoryState.unsupported)
    if not text:
        return SessionUsage(state=HistoryState.absent)
    roots = _roots(root, kind=kind, default=adapter.default_roots)
    try:
        return adapter.tally(text, roots=roots, max_bytes=max_bytes)
    except OSError:
        return SessionUsage(state=HistoryState.unreadable)
