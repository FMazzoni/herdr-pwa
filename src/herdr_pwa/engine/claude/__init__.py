"""The Claude Code reader: its records, where its log is, its pages, turns and spend."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import (
    Annotated,
    BinaryIO,
    ClassVar,
    Final,
    Literal,
    override,
)

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    RootModel,
    StrictStr,
    ValidationError,
)

from herdr_pwa.engine.model import SessionId
from herdr_pwa.engine.notices import (
    OUTPUT,
    PRINTED_NOTHING,
    SHELL,
    SLASH,
    SUMMARY,
    SUMMARY_LINE,
    CommandOutput,
    Hidden,
    Injected,
    PeerMessage,
    RecordOrigin,
    ShellCommand,
    SlashCommand,
    TaskNotice,
    api_error,
    injected,
    without_paste_tags,
    without_reminders,
)
from herdr_pwa.engine.transcript import (
    CLAUDE_TRANSCRIPT_ROOT,
    ENQUEUE,
    MAX_HISTORY_BYTES,
    MAX_NOTE_CHARS,
    MAX_PAGE_BYTES,
    MAX_PROSE_CHARS,
    MAX_RESULT_CHARS,
    MAX_USAGE_BYTES,
    NEWEST,
    POP_ALL,
    REMOVE,
    SPAWNED_AGENT,
    SPAWNED_SHELL,
    TAKEN,
    Gather,
    Harness,
    HistoryState,
    ModelRef,
    SessionUsage,
    Source,
    SpawnedAgent,
    Speaker,
    StartRecipe,
    ToolInput,
    TranscriptAdapter,
    TranscriptPage,
    TranscriptRoots,
    TranscriptTurn,
    TurnParser,
    Window,
    api_error_turn,
    clip,
    one_line,
    remember,
    search_roots,
    session_name,
    token_count,
    tool_summary,
)
from herdr_pwa.validation import OR_NONE, Count, Flag, Text

_CHUNK: Final = 256 * 1024

_HEAD_BYTES: Final = 64 * 1024

# Measured: the largest compact summary on this box is 28 KB, the record a cut stamp rides.
_HEAD_FINISH: Final = 256 * 1024

_FINISH_STEP: Final = 16 * 1024

_TAIL_BYTES: Final = 64 * 1024

_ANOTHER_FILE: Final = 1 << 62

_MAX_HOPS: Final = 8


class _ResultPart(BaseModel):
    """One part of a tool result's content list that the chat can show as text."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["text", "image"]
    text: Text = None


class _ResultParts(RootModel[Annotated[list[_ResultPart], Field(min_length=1)]]):
    """A tool result's content when it is text and images only; an empty list is not one."""


# Left to right, so only text and images read as parts; any other result stays whole JSON.
type _ResultBody = Annotated[
    StrictStr | _ResultParts | JsonValue, Field(union_mode="left_to_right")
]


class _Block(BaseModel):
    """One content block of a message: prose, a tool call, or a tool's result."""

    model_config = ConfigDict(extra="ignore")

    type: Text = None
    text: Text = None
    id: Text = None
    name: Text = None
    input: Annotated[ToolInput | None, OR_NONE] = None
    tool_use_id: Text = None
    is_error: Flag = None
    content: _ResultBody = None

    @property
    def said(self) -> str:
        """The block's text, empty when it has none."""
        return self.text or ""

    @property
    def called(self) -> str:
        """The tool a ``tool_use`` block calls, empty when it names none."""
        return self.name or ""


class _Details(BaseModel):
    model_config = ConfigDict(extra="ignore")

    thinking_tokens: Count = None


class _Usage(BaseModel):
    """What one assistant message cost, in tokens."""

    model_config = ConfigDict(extra="ignore")

    input_tokens: Count = None
    output_tokens: Count = None
    cache_creation_input_tokens: Count = None
    cache_read_input_tokens: Count = None
    output_tokens_details: Annotated[_Details | None, OR_NONE] = None


class _Message(BaseModel):
    """A record's ``message``: its content, and on an answer, what it cost."""

    model_config = ConfigDict(extra="ignore")

    content: Annotated[str | list[Annotated[_Block | None, OR_NONE]] | None, OR_NONE] = None
    id: Text = None
    model: Text = None
    usage: Annotated[_Usage | None, OR_NONE] = None


class _Kind(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: Text = None


class _Fork(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: Text = Field(default=None, validation_alias="sessionId")


class _Opaque(BaseModel):
    """An object this reader only needs to know is there."""

    model_config = ConfigDict(extra="ignore")


class _Queued(BaseModel):
    """An ``attachment`` record's body: a ``queued_command`` is what the harness delivered mid-turn."""

    model_config = ConfigDict(extra="ignore")

    type: Text = None
    prompt: Text = None
    origin: Annotated[RecordOrigin | None, OR_NONE] = None


class _Launch(BaseModel):
    """A tool result's ``toolUseResult``, as far as it says background work started or stopped."""

    model_config = ConfigDict(extra="ignore")

    status: Text = None
    agent_id: Text = Field(default=None, validation_alias="agentId")
    description: Text = None
    # A Bash call sent to the background, by its flag, its timeout or the person.
    shell: Text = Field(default=None, validation_alias="backgroundTaskId")
    # A message that woke an agent which had ended.
    resumed: Text = Field(
        default=None, validation_alias=AliasChoices("resumedAgentId", "revivedAgentId")
    )
    # TaskStop's result: the task it stopped and whether that was a shell or an agent.
    stopped: Text = Field(default=None, validation_alias="task_id")
    stopped_kind: Text = Field(default=None, validation_alias="task_type")


class ClaudeRecord(BaseModel):
    """One line of a Claude Code log, holding only the fields this reader looks at."""

    # Claude Code adds fields every release; one it renames reads as absent, never as an error.
    model_config = ConfigDict(extra="ignore")

    type: Text = None
    uuid: Text = None
    timestamp: Text = None
    subtype: Text = None
    # A system record's text, which is where a `local_command` one keeps its tags.
    content: Text = None
    cwd: Text = None
    is_meta: Flag = Field(default=None, validation_alias="isMeta")
    message: Annotated[_Message | None, OR_NONE] = None
    origin: Annotated[RecordOrigin | None, OR_NONE] = None
    prompt_source: Annotated[str | _Kind | None, OR_NONE] = Field(
        default=None, validation_alias="promptSource"
    )
    session: Text = Field(default=None, validation_alias="sessionId")
    # A background session's stamp is spelled the other way.
    stamp: Text = Field(default=None, validation_alias="session_id")
    session_kind: Text = Field(default=None, validation_alias="sessionKind")
    forked_from: Annotated[_Fork | None, OR_NONE] = Field(
        default=None, validation_alias="forkedFrom"
    )
    compact_metadata: Annotated[_Opaque | None, OR_NONE] = Field(
        default=None, validation_alias="compactMetadata"
    )
    is_compact_summary: Flag = Field(default=None, validation_alias="isCompactSummary")
    # Measured: the harness writes a refused turn as an assistant record from model "<synthetic>".
    is_api_error: Flag = Field(default=None, validation_alias="isApiErrorMessage")
    error: Text = None
    # A queue-operation record's verb; what it queued rides in `content`.
    operation: Text = None
    attachment: Annotated[_Queued | None, OR_NONE] = None
    tool_use_result: Annotated[_Launch | None, OR_NONE] = Field(
        default=None, validation_alias="toolUseResult"
    )


def claude_record(raw: bytes) -> ClaudeRecord | None:
    """One JSONL line as a record, or ``None`` for a line that is not one."""
    try:
        return ClaudeRecord.model_validate_json(raw)
    except ValidationError:
        return None


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


def _head_records(window: bytes) -> Iterator[ClaudeRecord]:
    """Every record *window* holds whole, in order."""
    # The trailing fragment is half a record past the budget, or empty at EOF.
    for raw in window.split(b"\n")[:-1]:
        yield _record(raw)


def _record(raw: bytes) -> ClaudeRecord:
    """One JSONL line as a record, or an empty one for anything that is not."""
    if not raw.strip():
        return ClaudeRecord()
    return claude_record(raw) or ClaudeRecord()


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
    for record in _head_records(window):
        value = record.stamp
        if not stamp and value and value != own:
            stamp = value
        if record.session_kind == "bg":
            return stamp or _first_stamp(window, own)
    return ""


def _first_stamp(window: bytes, own: str) -> str:
    """The first foreign ``session_id`` in *window*, once the kind is confirmed."""
    # Reached when the confirming record came before any stamp, so the first pass carried none.
    for record in _head_records(window):
        value = record.stamp
        if value and value != own:
            return value
    return ""


def _copied_from(path: Path) -> str:
    """The session *path*'s thread was COPIED OUT OF, per its own head window."""
    window = _head_window(path)
    head = next((r for r in _head_records(window) if r.session is not None), ClaudeRecord())
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
    window = _head_window(path)
    if b'"bg"' not in window:
        return False
    background = False
    compacted = False
    for record in _head_records(window):
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
        stamp = _record(raw).timestamp
        if stamp:
            return stamp
    return ""


def _last_writer(path: Path) -> str:
    """The session whose stamp is on *path*'s newest stamped record, from a bounded tail read."""
    for raw in reversed(_tail_window(path).split(b"\n")):
        stamp = _record(raw).stamp
        if stamp:
            return stamp
    return ""


# A head never changes once the file outgrows the window and its finish, so it is read once.
_heads: Final[dict[tuple[str, int, int, int], str]] = {}

_HEADS_MAX: Final = 4096


def _copied_from_cached(path: Path) -> str:
    """`_copied_from`, remembered per file for as long as its head window cannot change."""
    try:
        stat = path.stat()
    except OSError:
        return ""
    key = (str(path), stat.st_dev, stat.st_ino, min(stat.st_size, _HEAD_BYTES + _HEAD_FINISH))
    found = _heads.get(key)
    if found is None:
        if len(_heads) >= _HEADS_MAX:
            _heads.clear()
        found = _heads[key] = _copied_from(path)
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
    for _ in range(_MAX_HOPS):
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
    resolved = _contained(candidate, lineage.base)
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


def locate(
    session: SessionId | str,
    *,
    root: TranscriptRoots = None,
) -> Path | None:
    """The file *session*'s conversation is actually in now, or ``None``."""
    text = session_name(session)
    if not text:
        return None
    for base in search_roots(root, kind="claude"):
        # A root that cannot be read must not blank the roots after it.
        found = _in_root(base, text, follow=True)
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


def parse_claude_line(raw: bytes) -> TranscriptTurn | None:
    """One Claude Code JSONL line as a turn, or ``None`` for anything that is not one."""
    record = claude_record(raw)
    return _claude_turn(record) if record is not None else None


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


# The Claude Code adapter

_SKIP_TYPES: Final = frozenset(
    {
        "ai-title",
        "atis-latch",
        "file-history-delta",
        "file-history-snapshot",
        "frame-link",
        "last-prompt",
        "mode",
        "permission-mode",
        "system",
    }
)

_HUMAN_SOURCES: Final = frozenset({"typed", "queued", "pasted"})


def _source(record: ClaudeRecord) -> str | None:
    source = record.prompt_source
    return source.kind if isinstance(source, _Kind) else source


def _is_human(record: ClaudeRecord) -> bool:
    if _source(record) in _HUMAN_SOURCES:
        return True
    return record.origin is not None and record.origin.kind == "human"


def _blocks(record: ClaudeRecord) -> list[_Block]:
    """The blocks of *record*'s content, when it is a list of them."""
    content = record.message.content if record.message is not None else None
    if not isinstance(content, list):
        return []
    return [block for block in content if block is not None]


def _slash_turn(found: SlashCommand | CommandOutput, uuid: str) -> TranscriptTurn:
    """A slash command as one quiet line; its output is one too until it is folded onto it."""
    if isinstance(found, SlashCommand):
        text, cut = one_line(found.line, MAX_NOTE_CHARS)
        return TranscriptTurn(
            uuid=uuid,
            speaker=Speaker.notice,
            text=text,
            tool=SLASH,
            truncated=cut,
        )
    text, _ = one_line(found.said)
    result, cut = clip(found.said, MAX_RESULT_CHARS)
    return TranscriptTurn(
        uuid=uuid,
        speaker=Speaker.notice,
        text=text,
        tool=OUTPUT,
        result=result,
        result_truncated=cut,
        failed=found.failed,
        outcome=PRINTED_NOTHING if found.nothing else "",
    )


def _notice_turn(found: TaskNotice, uuid: str) -> TranscriptTurn:
    """A background task that stopped, as one quiet line that opens on what it said or ran."""
    detail, _ = one_line(found.detail)
    # A command opens on the whole of what was run; anything else on what it said.
    shown = found.detail if found.kind == "Command" else found.said
    result, cut = clip(shown, MAX_RESULT_CHARS)
    return TranscriptTurn(
        uuid=uuid,
        speaker=Speaker.notice,
        text=detail,
        tool=found.kind,
        result=result,
        result_truncated=cut,
        failed=found.failed,
        status=found.ended,
        elapsed_ms=found.elapsed_ms,
        task=found.task,
        outcome=found.outcome,
        # Only a command's spool is its output; an agent's is its whole transcript.
        output=found.output if found.kind == "Command" else "",
    )


def _injected_turn(
    found: TaskNotice | PeerMessage | SlashCommand | ShellCommand | CommandOutput, uuid: str
) -> TranscriptTurn:
    if isinstance(found, SlashCommand | CommandOutput):
        return _slash_turn(found, uuid)
    if isinstance(found, ShellCommand):
        # The person typed it at the agent's prompt: theirs, with what it printed opening below.
        text, cut = clip(found.line, MAX_PROSE_CHARS)
        return TranscriptTurn(
            uuid=uuid, speaker=Speaker.human, text=text, tool=SHELL, truncated=cut
        )
    if isinstance(found, TaskNotice):
        return _notice_turn(found, uuid)
    body, cut = clip(found.body, MAX_PROSE_CHARS)
    return TranscriptTurn(
        uuid=uuid,
        speaker=Speaker.peer,
        text=body,
        truncated=cut,
        sender=found.sender,
        report=found.report,
        task=found.task,
    )


def _summary_turn(content: str, uuid: str) -> TranscriptTurn:
    """A compact summary as a quiet line that opens on it, until it folds onto its /compact."""
    result, cut = clip(content, MAX_RESULT_CHARS)
    return TranscriptTurn(
        uuid=uuid,
        speaker=Speaker.notice,
        text=SUMMARY_LINE,
        tool=SUMMARY,
        result=result,
        result_truncated=cut,
    )


def _prompt(content: str) -> tuple[str, bool]:
    """A prompt the person sent as the chat shows it, clipped, with whether it was clipped."""
    return clip(without_paste_tags(without_reminders(content)), MAX_PROSE_CHARS)


def _user_text(record: ClaudeRecord, content: str, uuid: str) -> TranscriptTurn | None:
    """A user record whose content is one string: a prompt, or something the harness wrote."""
    if record.is_compact_summary is True:
        return _summary_turn(content, uuid)
    human = _is_human(record)
    found = injected(record.origin, content, human=human)
    if isinstance(found, Hidden):
        return None
    if found is not None:
        return _injected_turn(found, uuid)
    if record.is_meta:
        # Measured: a scheduled prompt is queued, then delivered hidden; only the queue reads it.
        return _queue_mark(TAKEN, content, uuid) if _source(record) else None
    # The cap follows the speaker: injected machinery gets the note cap, as the view shows three lines.
    text, cut = _prompt(content) if human else clip(without_reminders(content), MAX_NOTE_CHARS)
    if not text:
        return None
    return TranscriptTurn(
        uuid=uuid, speaker=Speaker.human if human else Speaker.system, text=text, truncated=cut
    )


def _user_record(record: ClaudeRecord, uuid: str) -> TranscriptTurn | None:
    """A ``role: "user"`` record, which is where the whole trap lives."""
    content = record.message.content if record.message is not None else None
    if isinstance(content, str):
        return _user_text(record, content, uuid)
    meta = record.is_meta is True
    for block in _blocks(record):
        turn = _user_block(block, uuid, meta=meta)
        if turn is not None:
            return turn
    return None


def _user_block(block: _Block, uuid: str, *, meta: bool) -> TranscriptTurn | None:
    """One content block of a user record, or ``None`` if it is not conversation."""
    kind = block.type
    if kind == "tool_result":
        text, cut = clip(_result_text(block.content), MAX_RESULT_CHARS)
        return TranscriptTurn(
            uuid=uuid,
            speaker=Speaker.tool,
            text=text,
            tool="result",
            truncated=cut,
            call=block.tool_use_id or "",
            failed=block.is_error is True,
        )
    if kind == "text" and not meta:
        text, cut = clip(block.said, MAX_NOTE_CHARS)
        if text:
            return TranscriptTurn(uuid=uuid, speaker=Speaker.system, text=text, truncated=cut)
    return None


def _result_text(body: _ResultBody) -> str:
    """A tool result as text; an inline image is named, never dumped as its base64."""
    if isinstance(body, str):
        return body
    if isinstance(body, _ResultParts):
        parts = body.root
        return "\n".join(part.text or "" if part.type == "text" else "[image]" for part in parts)
    return json.dumps(body, ensure_ascii=False)


def _api_error_turn(record: ClaudeRecord, uuid: str) -> TranscriptTurn:
    """A refused turn, opening on the harness's words."""
    said = "\n".join(block.said for block in _blocks(record) if block.type == "text")
    return api_error_turn(api_error(record.error, said), uuid)


def _assistant_record(record: ClaudeRecord, uuid: str) -> TranscriptTurn | None:
    """An assistant record: prose, or one collapsed tool call."""
    for block in _blocks(record):
        btype = block.type
        if btype == "text":
            text, cut = clip(block.said, MAX_PROSE_CHARS)
            if not text:
                return None
            return TranscriptTurn(uuid=uuid, speaker=Speaker.agent, text=text, truncated=cut)
        if btype == "tool_use":
            text, cut = tool_summary(block.input)
            return TranscriptTurn(
                uuid=uuid,
                speaker=Speaker.tool,
                text=text,
                tool=block.called,
                truncated=cut,
                call=block.id or "",
            )
        if btype == "thinking":
            # Measured: an empty `thinking` string and a signature blob, so nothing to render.
            return None
    return None


def _local_command(record: ClaudeRecord, uuid: str) -> TranscriptTurn | None:
    """A slash command, or its output, that Claude Code wrote as a system record."""
    found = injected(None, record.content or "", human=False)
    if isinstance(found, SlashCommand | CommandOutput):
        return _slash_turn(found, uuid)
    return None


# Measured: a notice or peer message delivered mid-turn is written only as this attachment.
_QUEUED: Final = "queued_command"


def _delivered(record: ClaudeRecord) -> TaskNotice | PeerMessage | None:
    """The task notice or peer message a queued-command attachment delivered, if it is one."""
    queued = record.attachment
    if queued is None or queued.type != _QUEUED:
        return None
    found = injected(queued.origin, queued.prompt or "", human=False)
    # A prompt the person queued is not read here: only what the harness delivered.
    return found if isinstance(found, TaskNotice | PeerMessage) else None


def _queued_prompt(queued: _Queued, uuid: str) -> TranscriptTurn | None:
    """A prompt the person typed mid-turn, which the harness writes only as this attachment."""
    if queued.type != _QUEUED or queued.origin is None or queued.origin.kind != "human":
        return None
    text, cut = _prompt(queued.prompt or "")
    if not text:
        return None
    return TranscriptTurn(uuid=uuid, speaker=Speaker.human, text=text, truncated=cut)


def _queued_turn(record: ClaudeRecord, uuid: str) -> TranscriptTurn | None:
    found = _delivered(record)
    if found is not None:
        return _injected_turn(found, uuid)
    return _queued_prompt(record.attachment, uuid) if record.attachment is not None else None


def _queue_mark(verb: str, content: str, uuid: str) -> TranscriptTurn | None:
    text, _ = _prompt(content)
    if verb != POP_ALL and not text:
        return None
    return TranscriptTurn(uuid=uuid, speaker=Speaker.queue, text=text, tool=verb)


def _queue_turn(record: ClaudeRecord) -> TranscriptTurn | None:
    """A queue operation as a mark the fold reads; the harness's own deliveries are left out."""
    verb, content = record.operation or "", record.content or ""
    if verb not in (ENQUEUE, REMOVE, POP_ALL):
        return None
    if verb == ENQUEUE and injected(None, content, human=False) is not None:
        return None
    return _queue_mark(verb, content, f"queue:{verb}:{record.timestamp}")


def _claude_turn(record: ClaudeRecord) -> TranscriptTurn | None:
    """One Claude Code JSONL record as a chat turn, or ``None`` to drop it."""
    kind = record.type
    if kind == "queue-operation":
        return _queue_turn(record)
    uuid = record.uuid
    if not uuid:
        return None
    # Measured: /artifacts, /branch, /feedback and others are written this way, same tags.
    if kind == "system" and record.subtype == "local_command":
        return _local_command(record, uuid)
    if kind == "attachment":
        return _queued_turn(record, uuid)
    if kind in _SKIP_TYPES or record.message is None:
        return None
    return _message_turn(record, uuid)


def _message_turn(record: ClaudeRecord, uuid: str) -> TranscriptTurn | None:
    """A record that carries a ``message``: the person's side, or the agent's."""
    if record.type == "user":
        return _user_record(record, uuid)
    if record.type == "assistant" and record.is_api_error is True:
        return _api_error_turn(record, uuid)
    if record.type == "assistant":
        return _assistant_record(record, uuid)
    return None


class ClaudeAdapter(TranscriptAdapter):
    """Claude Code: one JSONL per session, and the frontier is a BYTE OFFSET."""

    default_roots: ClassVar[tuple[str, ...]] = (CLAUDE_TRANSCRIPT_ROOT,)
    harness: ClassVar[Harness] = Harness(
        "claude",
        StartRecipe("claude", models=("fable", "opus", "sonnet", "haiku")),
        byte_frontier=True,
        # Claude Code writes tokens and no money, so a cost here would be invented.
        prices_itself=False,
    )

    @override
    def page(self, source: Source, window: Window) -> TranscriptPage:
        """The *window*'s turns or messages, ending at byte ``before``, oldest first."""
        path = self.log(source)
        if path is None:
            return TranscriptPage(state=HistoryState.absent)
        if window.max_bytes is None and window.messages:
            # Counting messages, the budget must reach past inline screenshots to find them.
            window = replace(window, max_bytes=MAX_HISTORY_BYTES)
        if source.expect and source.expect != path.stem:
            # A `before` offset into another file would be clamped, not refused, so drop it.
            page = read_page(path, replace(window, before=None))
            return page.model_copy(update={"reset": True, "moved": True})
        return read_page(path, window)

    @override
    def since(self, source: Source, window: Window) -> TranscriptPage:
        """Whatever has been APPENDED past byte ``after``, oldest first."""
        path = self.log(source)
        if path is None:
            return TranscriptPage(state=HistoryState.absent)
        if source.expect and source.expect != path.stem:
            # A held cursor is a promise about one file; reading it here would splice two threads.
            page = read_since(path, replace(window, after=_ANOTHER_FILE))
            return page.model_copy(update={"moved": True})
        return read_since(path, window)

    @override
    def tally(self, source: Source, *, max_bytes: int = MAX_USAGE_BYTES) -> SessionUsage:
        """What the session has spent, summed over its whole lineage."""
        path = self.log(source)
        if path is None:
            return SessionUsage(state=HistoryState.absent)
        return _tally_lineage(path, max_bytes=max_bytes)

    @override
    def origin(self, source: Source) -> str:
        """The first ``cwd`` the log records: where it began, not where a tool wandered."""
        path = self._log(source, follow=False)
        if path is None:
            return ""
        for record in _head_records(_head_window(path)):
            cwd = (record.cwd or "").strip()
            if cwd:
                return cwd
        return ""

    @classmethod
    def log(cls, source: Source) -> Path | None:
        """The file the session's conversation is in now, forks followed, or ``None``."""
        return cls._log(source, follow=True)

    @staticmethod
    def _log(source: Source, *, follow: bool) -> Path | None:
        """The session's log in the first root holding it, contained to THAT root."""
        if not source.session:
            return None
        for base in source.roots:
            found = _in_root(base, source.session, follow=follow)
            if found is not None:
                return found
        return None


_SYNTHETIC_MODEL: Final = "<synthetic>"


def _usage_of(record: ClaudeRecord) -> tuple[str, _Usage, str] | None:
    """An assistant record's ``(message id, usage, model)``, or ``None``."""
    message = record.message
    if record.type != "assistant" or message is None:
        return None
    if message.usage is None or not message.id:
        return None
    model = message.model or ""
    if model == _SYNTHETIC_MODEL:
        return None
    return message.id, message.usage, model


def _add_usage(tally: SessionUsage, usage: _Usage, model: str) -> None:
    """Fold one message's usage into *tally*, in place."""
    details = usage.output_tokens_details
    tally.messages += 1
    tally.input_tokens += token_count(usage.input_tokens)
    tally.output_tokens += token_count(usage.output_tokens)
    tally.cache_creation_tokens += token_count(usage.cache_creation_input_tokens)
    tally.cache_read_tokens += token_count(usage.cache_read_input_tokens)
    if details is not None:
        tally.thinking_tokens += token_count(details.thinking_tokens)
    if model:
        # Claude Code answers from one provider, so the reference carries none.
        remember(tally, ModelRef(model))


def _prompt_size(usage: _Usage) -> int:
    """Everything that went UP with one message: fresh, cache-written, cache-read."""
    return (
        token_count(usage.input_tokens)
        + token_count(usage.cache_creation_input_tokens)
        + token_count(usage.cache_read_input_tokens)
    )


class LiveTally(BaseModel):
    """A lineage's tally and what is needed to grow it by the lines appended to its newest log."""

    tally: SessionUsage
    seen: set[str] = Field(default_factory=set[str])
    budget: int = 0
    # Where the newest log's last whole line ends.
    end: int = 0
    # The newest Bash calls by tool_use id, so a result sent to the background is named.
    calls: dict[str, str] = Field(default_factory=dict[str, str])
    # Where each of the session's subagent logs was last read to.
    nested: dict[str, int] = Field(default_factory=dict[str, int])


# Measured: the bytes of a background launch, notice, backgrounded Bash call, resume or stop.
_SPAWN_MARKS: Final = (
    b'"async_launched"',
    b"<task-notification>",
    b'"queued_command"',
    b'"name":"Bash"',
    b'"name": "Bash"',
    b'"backgroundTaskId"',
    b'"resumedAgentId"',
    b'"revivedAgentId"',
    b'"task_type"',
)
# A call is answered within a few records; past this many an unanswered one is forgotten.
_HELD_CALLS: Final = 64
# What TaskStop says it stopped, as the tally's kinds.
_STOPPED: Final = MappingProxyType({"local_bash": SPAWNED_SHELL, "local_agent": SPAWNED_AGENT})
# What a notice calls each kind; measured, a stopped "Task" is a shell a subagent left running.
_NOTICE_KINDS: Final = MappingProxyType(
    {"Agent": SPAWNED_AGENT, "Command": SPAWNED_SHELL, "Task": SPAWNED_SHELL}
)
_BASH: Final = "Bash"


def _spawns(raw: bytes) -> bool:
    return any(mark in raw for mark in _SPAWN_MARKS)


def _worth(raw: bytes) -> bool:
    """Whether a line can carry usage, a prompt or background work; nothing else is parsed."""
    if b'"usage"' in raw or b'"promptSource"' in raw or b'"origin"' in raw:
        return True
    return _spawns(raw)


_ASYNC_LAUNCHED: Final = "async_launched"


def _spawned(tally: SessionUsage, task: str, kind: str = SPAWNED_AGENT) -> SpawnedAgent:
    """*task*'s entry in *tally*, added when this is the first record naming it."""
    for agent in tally.agents:
        if agent.task == task:
            return agent
    agent = SpawnedAgent(task=task, kind=kind)
    tally.agents.append(agent)
    return agent


def _settle(agent: SpawnedAgent) -> None:
    # Logs are read newest first, then their ancestors, so the latest stamps decide, not the order.
    agent.done = bool(agent.ended) and agent.ended >= agent.resumed


def _end(agent: SpawnedAgent, at: str, *, failed: bool) -> None:
    if not agent.ended or at > agent.ended:
        agent.ended, agent.failed = at, failed
    _settle(agent)


def _queued_notice(record: ClaudeRecord) -> Injected | None:
    # Measured: a subagent's shell ends only here; enqueue and remove repeat it, which is harmless.
    if record.type != "queue-operation" or record.operation not in (ENQUEUE, REMOVE):
        return None
    return injected(None, record.content or "", human=False)


def _ending(record: ClaudeRecord) -> TaskNotice | PeerMessage | None:
    """What *record* says ended a background agent or shell: its notice, or an agent's report."""
    found = _delivered(record) or _queued_notice(record)
    content = record.message.content if record.message is not None else None
    if found is None and record.type == "user" and isinstance(content, str):
        found = injected(record.origin, content, human=_is_human(record))
    if isinstance(found, TaskNotice):
        # A summary of no measured shape (a monitor's) names no kind this tally keeps.
        ends = found.named and found.kind in _NOTICE_KINDS and not found.waiting
        return found if ends else None
    return found if isinstance(found, PeerMessage) and found.report else None


def _launched(record: ClaudeRecord, tally: SessionUsage) -> bool:
    launch = record.tool_use_result
    if launch is None or launch.status != _ASYNC_LAUNCHED or not launch.agent_id:
        return False
    agent = _spawned(tally, launch.agent_id)
    agent.description = launch.description or agent.description
    agent.started = record.timestamp or agent.started
    return True


def _shell_name(block: _Block) -> str:
    args = block.input
    given = (args.description or args.command or "") if args is not None else ""
    lines = given.strip().splitlines()
    return lines[0] if lines else ""


def _note_calls(record: ClaudeRecord, live: LiveTally) -> None:
    """Hold each Bash call's name until its result says whether it went to the background."""
    if record.type != "assistant":
        return
    for block in _blocks(record):
        if block.type == "tool_use" and block.name == _BASH and block.id:
            live.calls[block.id] = _shell_name(block)
    while len(live.calls) > _HELD_CALLS:
        live.calls.pop(next(iter(live.calls)))


def _shell_launched(record: ClaudeRecord, live: LiveTally) -> bool:
    launch = record.tool_use_result
    if launch is None or not launch.shell:
        return False
    shell = _spawned(live.tally, launch.shell, SPAWNED_SHELL)
    shell.kind = SPAWNED_SHELL
    answered = [b.tool_use_id for b in _blocks(record) if b.type == "tool_result" and b.tool_use_id]
    named = [live.calls.pop(call, "") for call in answered]
    shell.description = next((name for name in named if name), shell.description)
    shell.started = record.timestamp or shell.started
    return True


def _resumed(record: ClaudeRecord, tally: SessionUsage) -> bool:
    launch = record.tool_use_result
    at = record.timestamp or ""
    if launch is None or not launch.resumed or not at:
        return False
    agent = _spawned(tally, launch.resumed)
    agent.resumed = max(agent.resumed, at)
    _settle(agent)
    return True


def _stopped(record: ClaudeRecord, tally: SessionUsage) -> bool:
    launch = record.tool_use_result
    kind = _STOPPED.get(launch.stopped_kind or "") if launch is not None else None
    if launch is None or kind is None or not launch.stopped:
        return False
    _end(_spawned(tally, launch.stopped, kind), record.timestamp or "", failed=False)
    return True


def _started_or_stopped(record: ClaudeRecord, live: LiveTally) -> bool:
    tally = live.tally
    if _launched(record, tally) or _shell_launched(record, live):
        return True
    return _resumed(record, tally) or _stopped(record, tally)


def _tally_spawned(record: ClaudeRecord, live: LiveTally) -> None:
    """Add a background agent's or shell's launch, resume or ending to *live*, if *record* is one."""
    _note_calls(record, live)
    if _started_or_stopped(record, live):
        return
    found = _ending(record)
    if found is None or not found.task:
        return
    notice = found if isinstance(found, TaskNotice) else None
    kind = _NOTICE_KINDS[notice.kind] if notice is not None else SPAWNED_AGENT
    agent = _spawned(live.tally, found.task, kind)
    if notice is not None and not agent.description:
        agent.description = notice.detail
    _end(agent, record.timestamp or "", failed=notice is not None and notice.failed)


def _nested_logs(path: Path) -> list[Path]:
    """The logs of the agents *path*'s session started, which Claude Code keeps beside it."""
    directory = path.with_suffix("") / "subagents"
    try:
        return sorted(directory.glob("agent-*.jsonl"))
    except OSError:
        return []


def _tally_nested_log(path: Path, live: LiveTally) -> None:
    """Add the background work in one subagent log's whole lines appended since it was read."""
    key = path.name
    start = live.nested.get(key, 0)
    with path.open("rb") as handle:
        if handle.seek(0, 2) < start:
            # Rewritten, not appended to: read it again whole.
            start = 0
        handle.seek(start)
        for raw in handle:
            if not raw.endswith(b"\n"):
                break
            start += len(raw)
            if _spawns(raw):
                _tally_spawned(_record(raw), live)
    live.nested[key] = start


def _tally_nested(path: Path, live: LiveTally) -> None:
    """Add what the session's own agents started; their usage is not the session's, so only that."""
    for log in _nested_logs(path):
        try:
            _tally_nested_log(log, live)
        except OSError:
            continue


def _tally_log(path: Path, live: LiveTally, *, newest: bool, start: int = 0) -> int:
    """Add *path*'s whole lines from *start* to *live*; return where the last of them ends."""
    end = start
    with path.open("rb") as handle:
        handle.seek(start)
        for raw in handle:
            if not raw.endswith(b"\n"):
                # A line still being written is read once it is whole.
                break
            end += len(raw)
            _tally_raw(raw, live, newest=newest)
    return end


def _tally_raw(raw: bytes, live: LiveTally, *, newest: bool) -> None:
    """Add one whole line to *live*: all of it inside the byte budget, its background work past it."""
    live.budget -= len(raw)
    if live.budget >= 0:
        if _worth(raw):
            _tally_line(raw, live, newest=newest)
        return
    live.tally.partial = True
    live.budget = 0
    # Measured: a shell's end lands anywhere in a long log, so a spent budget may never hide it.
    if _spawns(raw):
        _tally_spawned(_record(raw), live)


def _tally_prompt(
    record: ClaudeRecord, tally: SessionUsage, seen: set[str], *, newest: bool
) -> None:
    """Count one prompt the person sent; the newest log's last one starts the current turn."""
    if newest:
        tally.turn_started = record.timestamp or ""
    key = f"prompt:{record.uuid}"
    if key not in seen:
        seen.add(key)
        tally.prompts += 1


def _tally_line(raw: bytes, live: LiveTally, *, newest: bool) -> None:
    """Add one record to *live*: its message unless already counted, or the prompt it is."""
    record = _record(raw)
    tally, seen = live.tally, live.seen
    if newest and record.timestamp:
        tally.last_at = record.timestamp
    _tally_spawned(record, live)
    if record.type == "user" and not record.is_meta and _is_human(record):
        _tally_prompt(record, tally, seen, newest=newest)
        return
    found = _usage_of(record)
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


def tally_claude(path: Path, *, max_bytes: int = MAX_USAGE_BYTES) -> LiveTally:
    """Sum *path* and every log its thread was copied out of, ready to grow."""
    live = LiveTally(tally=SessionUsage(), budget=max_bytes)
    for index, log in enumerate([path, *_ancestors(path)]):
        latest = live.tally.latest
        end = _tally_log(log, live, newest=index == 0)
        if latest.model:
            # Logs are read newest first: an older one's answers never name the current model.
            live.tally.latest = latest
        if index == 0:
            live.end = end
    _tally_nested(path, live)
    live.tally.state = HistoryState.ok if live.tally.messages else HistoryState.empty
    return live


def grow_claude(live: LiveTally, path: Path) -> None:
    """Add what was appended to the newest log, and to its agents' logs, since *live* read them."""
    live.end = _tally_log(path, live, newest=True, start=live.end)
    live.tally.state = HistoryState.ok if live.tally.messages else HistoryState.empty
    # An agent's shell ends in that agent's log while the session's own is still.
    _tally_nested(path, live)


def _tally_lineage(path: Path, *, max_bytes: int) -> SessionUsage:
    """Sum *path* and every log its thread was copied out of."""
    return tally_claude(path, max_bytes=max_bytes).tally
