"""The provider transcript: the only source that is a conversation, not a screen."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Annotated, BinaryIO, ClassVar, Final, NamedTuple, Protocol, override

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from herdr_pwa.diff import EditArgs, FileDiff, tool_diff
from herdr_pwa.engine.generated.request import IntegrationTarget
from herdr_pwa.engine.model import Agent, SessionId
from herdr_pwa.engine.notices import (
    API_ERROR_KINDS,
    COMPACT_COMMAND,
    OUTPUT,
    SLASH,
    SLASH_KINDS,
    SUMMARY,
    SUMMARY_LINE,
    ApiError,
    CommandOutput,
    Hidden,
    PeerMessage,
    RecordOrigin,
    SlashCommand,
    TaskNotice,
    api_error,
    injected,
    without_reminders,
)
from herdr_pwa.validation import OR_NONE, Count, Flag, Text

CLAUDE_TRANSCRIPT_ROOT: Final = "~/.claude/projects"

# Every kind herdr itself can run, whether or not a harness here reads it.
HERDR_KINDS: Final = tuple(target.value for target in IntegrationTarget)

# A path, or the string an env var carries.
type OneRoot = str | os.PathLike[str]

# One root, several searched in order, or the provider's default.
type TranscriptRoots = OneRoot | Sequence[OneRoot] | None

type TurnParser = Callable[[bytes], TranscriptTurn | None]

MAX_PAGE_BYTES: Final = 4 * 1024 * 1024

# Measured: a real session stores screenshots inline, so twenty messages sat 18MB from the end.
MAX_HISTORY_BYTES: Final = 64 * 1024 * 1024

# A page of messages still stops here, so a long tool burst cannot make one page unbounded.
MAX_PAGE_TURNS: Final = 400

MAX_PROSE_CHARS: Final = 12000

MAX_RESULT_CHARS: Final = 2000

MAX_NOTE_CHARS: Final = 2000

MAX_SUMMARY_CHARS: Final = 160

MAX_USAGE_BYTES: Final = 64 * 1024 * 1024

_CHUNK: Final = 256 * 1024

_HEAD_BYTES: Final = 64 * 1024

# Measured: the largest compact summary on this box is 28 KB, the record a cut stamp rides.
_HEAD_FINISH: Final = 256 * 1024

_FINISH_STEP: Final = 16 * 1024

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

    # A background task stopped, or the API refused a turn: one quiet line, never a prompt.
    notice = "notice"

    # Another agent's message or hand-back report, written into this log by the harness.
    peer = "peer"


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
    # The frontier just before this turn: `before=at` leaves it out, `after=at` starts with it.
    at: int = -1
    # How a task notice says its task ended ("finished", "exit 0") and how long it ran; its
    # kind ("Agent", "Command", "Task") rides in `tool`, the label a tool's line leads with.
    status: str = ""
    elapsed_ms: int = 0
    # A command's ending in the harness's words, and the file its output went to, never read.
    outcome: str = ""
    output: str = ""
    # Who a peer message is from, and whether it is a subagent's hand-back report.
    sender: str = ""
    report: bool = False
    # A task notice's task id, or the task a peer message came from: what joins the two.
    task: str = ""
    # How many identical API errors this one line stands for, retries folded together.
    times: int = 1


def is_message(turn: TranscriptTurn) -> bool:
    """Whether *turn* is a message a page counts: a person's prompt or the agent's prose."""
    return turn.speaker in (Speaker.human, Speaker.agent)


def is_slash_command(turn: TranscriptTurn) -> bool:
    """Whether *turn* is a slash command the person ran in the harness, or what one printed."""
    return turn.speaker is Speaker.notice and turn.tool in SLASH_KINDS


def is_compact_summary(turn: TranscriptTurn) -> bool:
    """Whether *turn* is the harness's summary of what a compaction dropped, not a word said."""
    return turn.speaker is Speaker.notice and turn.tool == SUMMARY


def is_api_error(turn: TranscriptTurn) -> bool:
    """Whether *turn* is the harness saying a turn ended with no answer, not the agent speaking."""
    return turn.speaker is Speaker.notice and turn.tool in API_ERROR_KINDS


class TurnStamp(NamedTuple):
    """The record a turn is drawn from: its id, and when it was written."""

    uuid: str
    written: str


@dataclass(frozen=True, slots=True)
class Window:
    """Which stretch of a conversation one read returns, and what it may spend finding it."""

    limit: int = 40
    # A page ends at frontier `before`; a read past `after` returns only what was added since.
    before: int | None = None
    after: int | None = None
    # Above 0, a read also stops once it holds this many messages.
    messages: int = 0
    # None leaves the budget to the reader.
    max_bytes: int | None = None

    @property
    def budget(self) -> int:
        """The bytes this read may spend: the asked budget, or one page's."""
        return MAX_PAGE_BYTES if self.max_bytes is None else self.max_bytes


# What a read that names no window asks for: the newest page of turns.
NEWEST: Final = Window()


@dataclass(frozen=True, slots=True)
class Source:
    """Where one session's conversation is read from, and which log a held frontier was read in."""

    session: str
    roots: Sequence[Path] = ()
    # A frontier is a promise about one log; read in another, it must not be honoured.
    expect: str = ""
    # The harness whose adapter reads it, set by `of`; an adapter called directly never looks.
    kind: str = ""

    @classmethod
    def of(
        cls,
        session: SessionId | str,
        *,
        agent_kind: str | None = "claude",
        root: TranscriptRoots = None,
        log: str = "",
    ) -> Source:
        """*session* of harness *agent_kind*, searched for under the configured *root*."""
        kind = (agent_kind or "").strip().lower()
        adapter = adapter_for(kind)
        roots = (
            () if adapter is None else tuple(_roots(root, kind=kind, default=adapter.default_roots))
        )
        # A session id off a URL segment must not walk the tree, so a path reads as no session.
        return cls(_session_id(session), roots, expect=log, kind=kind)


class Gather:
    """The turns a page has taken so far, and whether it is full by turns or by messages."""

    def __init__(self, window: Window) -> None:
        self.limit = window.limit
        self.messages = window.messages
        self.budget = window.budget
        self.turns: list[TranscriptTurn] = []
        self.said = 0

    def take(self, turn: TranscriptTurn | None, at: int) -> bool:
        """Keep *turn* if there is one, marked with frontier *at*; true once the page is full."""
        if turn is None:
            return False
        turn.at = at
        self.turns.append(turn)
        self.said += is_message(turn)
        return len(self.turns) >= self.limit or 0 < self.messages <= self.said


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


# What a Claude Code record holds, as far as this reader looks


class ToolInput(EditArgs):
    """A tool call's arguments: the one that names the call, and any file it changes."""

    command: Text = None
    pattern: Text = None
    query: Text = None
    url: Text = None
    description: Text = None
    summary: Text = None
    skill: Text = None
    subagent_type: Text = None
    name: Text = None
    prompt: Text = None
    text: Text = None

    def naming(self) -> tuple[str | None, ...]:
        """The arguments that can name the call, most telling first, as Collie's summary orders them."""
        return (
            self.command,
            self.file_path,
            self.notebook_path,
            self.path,
            self.pattern,
            self.query,
            self.url,
            self.description,
            self.summary,
            self.skill,
            self.subagent_type,
            self.name,
            self.prompt,
            self.text,
        )


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
    # Kept whole: a result that is not text and images is shown as the JSON it was.
    content: JsonValue = None

    @property
    def said(self) -> str:
        """The block's text, empty when it has none."""
        return self.text or ""

    @property
    def called(self) -> str:
        """The tool a ``tool_use`` block calls, empty when it names none."""
        return self.name or ""


class _ResultPart(BaseModel):
    """One part of a tool result's content list."""

    model_config = ConfigDict(extra="ignore")

    type: Text = None
    text: Text = None


_RESULT_PARTS: Final = TypeAdapter(list[_ResultPart])


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
    """A tool result's ``toolUseResult``, as far as it says a background agent was started."""

    model_config = ConfigDict(extra="ignore")

    status: Text = None
    agent_id: Text = Field(default=None, validation_alias="agentId")
    description: Text = None


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


# Locating the file


def _for_kind(entry: str, kind: str) -> str:
    """*entry* as it applies to *kind*: unqualified, qualified for it, or empty."""
    head, sep, rest = entry.partition(":")
    if not sep or head.strip().lower() not in _registry():
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
_HEADS: Final[dict[tuple[str, int, int, int], str]] = {}

_HEADS_MAX: Final = 4096


def _copied_from_cached(path: Path) -> str:
    """`_copied_from`, remembered per file for as long as its head window cannot change."""
    try:
        stat = path.stat()
    except OSError:
        return ""
    key = (str(path), stat.st_dev, stat.st_ino, min(stat.st_size, _HEAD_BYTES + _HEAD_FINISH))
    found = _HEADS.get(key)
    if found is None:
        if len(_HEADS) >= _HEADS_MAX:
            _HEADS.clear()
        found = _HEADS[key] = _copied_from(path)
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


@dataclass(slots=True)
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
) -> Path | None:
    """The file *session*'s conversation is actually in now, or ``None``."""
    text = _session_id(session)
    if not text:
        return None
    for base in _roots(root, kind="claude"):
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
        "queue-operation",
        "system",
    }
)

_HUMAN_SOURCES: Final = frozenset({"typed", "queued", "pasted"})


def _is_human(record: ClaudeRecord) -> bool:
    source = record.prompt_source
    said = source.kind if isinstance(source, _Kind) else source
    if said in _HUMAN_SOURCES:
        return True
    return record.origin is not None and record.origin.kind == "human"


def clip(text: str, limit: int) -> tuple[str, bool]:
    """*text* stripped and cut to *limit* characters, with whether it was cut."""
    text = text.strip()
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def one_line(value: str | None, limit: int = MAX_SUMMARY_CHARS) -> tuple[str, bool]:
    """*value* as a single collapsed line, clipped, with whether it was clipped."""
    if value is None:
        return "", False
    return clip(" ".join(value.split()), limit)


def first_line(values: Iterable[str | None], limit: int = MAX_SUMMARY_CHARS) -> tuple[str, bool]:
    """The first of *values* with something on it, as `one_line`."""
    for value in values:
        line, cut = one_line(value, limit)
        if line:
            return line, cut
    return "", False


def tool_summary(args: ToolInput | None) -> tuple[str, bool]:
    """The one argument that identifies a tool call (see `ToolInput.naming`)."""
    return ("", False) if args is None else first_line(args.naming())


def _blocks(record: ClaudeRecord) -> list[_Block]:
    """The blocks of *record*'s content, when it is a list of them."""
    content = record.message.content if record.message is not None else None
    if not isinstance(content, list):
        return []
    return [block for block in content if block is not None]


def _slash_turn(found: SlashCommand | CommandOutput, stamp: TurnStamp) -> TranscriptTurn:
    """A slash command as one quiet line; its output is one too until it is folded onto it."""
    if isinstance(found, SlashCommand):
        text, cut = one_line(found.line, MAX_NOTE_CHARS)
        return TranscriptTurn(
            uuid=stamp.uuid,
            speaker=Speaker.notice,
            text=text,
            ts=stamp.written,
            tool=SLASH,
            truncated=cut,
        )
    text, _ = one_line(found.said)
    result, cut = clip(found.said, MAX_RESULT_CHARS)
    return TranscriptTurn(
        uuid=stamp.uuid,
        speaker=Speaker.notice,
        text=text,
        ts=stamp.written,
        tool=OUTPUT,
        result=result,
        result_truncated=cut,
        failed=found.failed,
    )


def _injected_turn(
    found: TaskNotice | PeerMessage | SlashCommand | CommandOutput, stamp: TurnStamp
) -> TranscriptTurn:
    uuid, ts = stamp.uuid, stamp.written
    if isinstance(found, SlashCommand | CommandOutput):
        return _slash_turn(found, stamp)
    if isinstance(found, TaskNotice):
        detail, _ = one_line(found.detail)
        # A command opens on the whole of what was run; anything else on what it said.
        shown = found.detail if found.kind == "Command" else found.said
        result, cut = clip(shown, MAX_RESULT_CHARS)
        return TranscriptTurn(
            uuid=uuid,
            speaker=Speaker.notice,
            text=detail,
            ts=ts,
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
    body, cut = clip(found.body, MAX_PROSE_CHARS)
    return TranscriptTurn(
        uuid=uuid,
        speaker=Speaker.peer,
        text=body,
        ts=ts,
        truncated=cut,
        sender=found.sender,
        report=found.report,
        task=found.task,
    )


def _summary_turn(content: str, stamp: TurnStamp) -> TranscriptTurn:
    """A compact summary as a quiet line that opens on it, until it folds onto its /compact."""
    result, cut = clip(content, MAX_RESULT_CHARS)
    return TranscriptTurn(
        uuid=stamp.uuid,
        speaker=Speaker.notice,
        text=SUMMARY_LINE,
        ts=stamp.written,
        tool=SUMMARY,
        result=result,
        result_truncated=cut,
    )


def _user_text(record: ClaudeRecord, content: str, stamp: TurnStamp) -> TranscriptTurn | None:
    """A user record whose content is one string: a prompt, or something the harness wrote."""
    if record.is_compact_summary is True:
        return _summary_turn(content, stamp)
    human = _is_human(record)
    found = injected(record.origin, content, human=human)
    if isinstance(found, Hidden):
        return None
    if found is not None:
        return _injected_turn(found, stamp)
    if record.is_meta:
        return None
    # The cap follows the speaker: injected machinery gets the note cap, as the view shows three lines.
    text, cut = clip(without_reminders(content), MAX_PROSE_CHARS if human else MAX_NOTE_CHARS)
    if not text:
        return None
    return TranscriptTurn(
        uuid=stamp.uuid,
        speaker=Speaker.human if human else Speaker.system,
        text=text,
        ts=stamp.written,
        truncated=cut,
    )


def _user_record(record: ClaudeRecord, stamp: TurnStamp) -> TranscriptTurn | None:
    """A ``role: "user"`` record, which is where the whole trap lives."""
    content = record.message.content if record.message is not None else None
    if isinstance(content, str):
        return _user_text(record, content, stamp)
    meta = record.is_meta is True
    for block in _blocks(record):
        turn = _user_block(block, stamp, meta=meta)
        if turn is not None:
            return turn
    return None


def _user_block(block: _Block, stamp: TurnStamp, *, meta: bool) -> TranscriptTurn | None:
    """One content block of a user record, or ``None`` if it is not conversation."""
    uuid, ts = stamp.uuid, stamp.written
    kind = block.type
    if kind == "tool_result":
        text, cut = clip(_result_text(block.content), MAX_RESULT_CHARS)
        return TranscriptTurn(
            uuid=uuid,
            speaker=Speaker.tool,
            text=text,
            ts=ts,
            tool="result",
            truncated=cut,
            call=block.tool_use_id or "",
            failed=block.is_error is True,
        )
    if kind == "text" and not meta:
        text, cut = clip(block.said, MAX_NOTE_CHARS)
        if text:
            return TranscriptTurn(
                uuid=uuid, speaker=Speaker.system, text=text, ts=ts, truncated=cut
            )
    return None


def _result_text(body: JsonValue) -> str:
    """A tool result as text; an inline image is named, never dumped as its base64."""
    if isinstance(body, str):
        return body
    parts = _result_parts(body)
    if not parts or any(part.type not in ("text", "image") for part in parts):
        return json.dumps(body, ensure_ascii=False)
    return "\n".join(part.text or "" if part.type == "text" else "[image]" for part in parts)


def _result_parts(body: JsonValue) -> list[_ResultPart]:
    """*body* as a list of result parts, or none when it is not one."""
    if not isinstance(body, list):
        return []
    try:
        return _RESULT_PARTS.validate_python(body)
    except ValidationError:
        return []


def api_error_turn(found: ApiError, stamp: TurnStamp) -> TranscriptTurn:
    """A turn that ended with no answer as a quiet line saying what unblocks it."""
    line, _ = one_line(found.line)
    result, cut = clip(found.said, MAX_RESULT_CHARS)
    return TranscriptTurn(
        uuid=stamp.uuid,
        speaker=Speaker.notice,
        text=line,
        ts=stamp.written,
        tool=found.kind,
        result="" if result == line else result,
        result_truncated=cut,
        failed=found.failed,
    )


def _api_error_turn(record: ClaudeRecord, stamp: TurnStamp) -> TranscriptTurn:
    """A refused turn, opening on the harness's words."""
    said = "\n".join(block.said for block in _blocks(record) if block.type == "text")
    return api_error_turn(api_error(record.error, said), stamp)


def _assistant_record(record: ClaudeRecord, stamp: TurnStamp) -> TranscriptTurn | None:
    """An assistant record: prose, or one collapsed tool call."""
    uuid, ts = stamp.uuid, stamp.written
    for block in _blocks(record):
        btype = block.type
        if btype == "text":
            text, cut = clip(block.said, MAX_PROSE_CHARS)
            if not text:
                return None
            return TranscriptTurn(uuid=uuid, speaker=Speaker.agent, text=text, ts=ts, truncated=cut)
        if btype == "tool_use":
            text, cut = tool_summary(block.input)
            return TranscriptTurn(
                uuid=uuid,
                speaker=Speaker.tool,
                text=text,
                ts=ts,
                tool=block.called,
                truncated=cut,
                call=block.id or "",
                diff=tool_diff(block.input),
            )
        if btype == "thinking":
            # Measured: an empty `thinking` string and a signature blob, so nothing to render.
            return None
    return None


def _local_command(record: ClaudeRecord, stamp: TurnStamp) -> TranscriptTurn | None:
    """A slash command, or its output, that Claude Code wrote as a system record."""
    found = injected(None, record.content or "", human=False)
    if isinstance(found, SlashCommand | CommandOutput):
        return _slash_turn(found, stamp)
    return None


# Measured: a notice or a peer's message delivered while a turn runs is written only as an
# attachment of this type (943 notices and 140 messages on this box, 9 also as user records).
_QUEUED: Final = "queued_command"


def _delivered(record: ClaudeRecord) -> TaskNotice | PeerMessage | None:
    """The task notice or peer message a queued-command attachment delivered, if it is one."""
    queued = record.attachment
    if queued is None or queued.type != _QUEUED:
        return None
    found = injected(queued.origin, queued.prompt or "", human=False)
    # A prompt the person queued is not read here: only what the harness delivered.
    return found if isinstance(found, TaskNotice | PeerMessage) else None


def _queued_turn(record: ClaudeRecord, stamp: TurnStamp) -> TranscriptTurn | None:
    found = _delivered(record)
    return _injected_turn(found, stamp) if found is not None else None


def _claude_turn(record: ClaudeRecord) -> TranscriptTurn | None:
    """One Claude Code JSONL record as a chat turn, or ``None`` to drop it."""
    kind = record.type
    uuid = record.uuid
    if not uuid:
        return None
    stamp = TurnStamp(uuid, record.timestamp or "")
    # Measured: /artifacts, /branch, /feedback and others are written this way, same tags.
    if kind == "system" and record.subtype == "local_command":
        return _local_command(record, stamp)
    if kind == "attachment":
        return _queued_turn(record, stamp)
    if kind in _SKIP_TYPES or record.message is None:
        return None
    return _message_turn(record, stamp)


def _message_turn(record: ClaudeRecord, stamp: TurnStamp) -> TranscriptTurn | None:
    """A record that carries a ``message``: the person's side, or the agent's."""
    if record.type == "user":
        return _user_record(record, stamp)
    if record.type == "assistant" and record.is_api_error is True:
        return _api_error_turn(record, stamp)
    if record.type == "assistant":
        return _assistant_record(record, stamp)
    return None


def fold_turns(turns: Iterable[TranscriptTurn]) -> list[TranscriptTurn]:
    """Fold tool results onto calls, command output and a compaction onto its command, and
    repeated API errors."""
    return _fold_repeats(_fold_compaction(_fold_outputs(_fold_results(turns))))


def _prints_for(output: TranscriptTurn, command: TranscriptTurn) -> bool:
    """Whether *output* is what *command* printed: measured, it is the very next record."""
    return (
        is_slash_command(output)
        and output.tool == OUTPUT
        and is_slash_command(command)
        and command.tool == SLASH
        and not command.result
    )


def _fold_outputs(turns: list[TranscriptTurn]) -> list[TranscriptTurn]:
    """Each slash command's OUTPUT folded onto the command just before it, where it opens."""
    folded: list[TranscriptTurn] = []
    for turn in turns:
        if folded and _prints_for(turn, folded[-1]):
            update = {
                "result": turn.result,
                "result_truncated": turn.result_truncated,
                "failed": turn.failed,
            }
            folded[-1] = folded[-1].model_copy(update=update)
            continue
        folded.append(turn)
    return folded


def _runs_compact(turn: TranscriptTurn) -> bool:
    return (
        is_slash_command(turn) and turn.tool == SLASH and turn.text.split(" ")[0] == COMPACT_COMMAND
    )


@dataclass(slots=True)
class _Compaction:
    """What waits for the /compact line: the summary, and the line as the person sent it."""

    summary: TranscriptTurn | None = None
    sent: TranscriptTurn | None = None


def _compaction_step(turn: TranscriptTurn, waiting: _Compaction) -> list[TranscriptTurn]:
    """What *turn* takes in out of what waits for it, emptying what it ends."""
    if is_compact_summary(turn):
        waiting.summary = turn
    elif turn.speaker is Speaker.system:
        waiting.sent = turn
    elif turn.speaker is Speaker.human or _runs_compact(turn):
        taken = [t for t in (waiting.summary, waiting.sent) if t is not None]
        waiting.summary = waiting.sent = None
        return taken if _runs_compact(turn) else []
    return []


def _fold_compaction(turns: list[TranscriptTurn]) -> list[TranscriptTurn]:
    """Each /compact line opening on the summary before it, the line as the person sent it dropped.

    Measured: the line as sent is written at once, then, once the compaction is done, the
    summary, the command and what it printed; a background copy keeps the agent's last words
    between the summary and the command, so only a prompt the person typed ends the wait.
    """
    waiting = _Compaction()
    taken: dict[str, TranscriptTurn] = {}
    dropped: set[str] = set()
    for turn in turns:
        for earlier in _compaction_step(turn, waiting):
            if is_compact_summary(earlier):
                taken[turn.uuid] = earlier
                dropped.add(earlier.uuid)
            elif earlier.text == turn.text:
                dropped.add(earlier.uuid)
    return [_opening_on(turn, taken.get(turn.uuid)) for turn in turns if turn.uuid not in dropped]


def _opening_on(turn: TranscriptTurn, summary: TranscriptTurn | None) -> TranscriptTurn:
    """*turn*, opening on *summary* in place of "Compacted (ctrl+o to see full summary)"."""
    if summary is None:
        return turn
    update = {"result": summary.result, "result_truncated": summary.result_truncated}
    return turn.model_copy(update=update)


def _fold_results(turns: Iterable[TranscriptTurn]) -> list[TranscriptTurn]:
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


@dataclass(frozen=True, slots=True)
class StartRecipe:
    """How herdr starts one harness from the phone: its executable, model flag, models, environment."""

    # herdr's ``--kind`` names the canonical executable, so this is what must be on PATH.
    binary: str
    model_flag: str = "--model"
    # Empty means this box cannot enumerate them, so no model is ever put on the argv.
    models: tuple[str, ...] = ()
    # The ``Settings`` field the operator declares the models in, for a harness nothing on this
    # box can be asked for its own; empty means ``models`` is the whole list.
    models_setting: str = ""
    env: Mapping[str, str] = field(default_factory=dict[str, str])
    # The flag that hands the TUI its first prompt at launch; empty means it is typed once it
    # listens. Only for a TUI on the alternate screen: the shell's own echo of the command line
    # would otherwise read back as the prompt having landed.
    prompt_flag: str = ""

    @property
    def takes_prompt(self) -> bool:
        """Whether the first prompt goes on the command line instead of being typed."""
        return bool(self.prompt_flag)

    def args(self, model: str, prompt: str = "") -> tuple[str, ...]:
        """What follows the bare ``--`` on the agent's command line."""
        chosen = (self.model_flag, model) if model else ()
        # One element, ``flag=text``, so a prompt that starts with a dash is never an option.
        opening = (f"{self.prompt_flag}={prompt}",) if prompt and self.prompt_flag else ()
        return chosen + opening


@dataclass(frozen=True, slots=True)
class Harness:
    """What one harness is and can do. Callers ask these, never the kind's name."""

    # herdr's name for it, the key the registry is looked up by.
    kind: str
    launch: StartRecipe
    # One append-only JSONL per session paged by byte: the tail and usage caches read it in place
    # and grow it by what was appended, and a stat of the file says whether it moved.
    byte_frontier: bool = False
    # Whether the tally's context figure is the prompt's size, rather than nothing stored.
    stores_context: bool = True
    # Whether the tally's cost is the harness's own price; one recording no money reads zero.
    prices_itself: bool = True


# What the harness delivers between retries: each one sets off the next refused turn.
_RETRY_TRIGGERS: Final = (Speaker.notice, Speaker.peer)


def _wakes(turn: TranscriptTurn) -> bool:
    """Whether *turn* only woke the agent to retry; a slash command is the person acting.

    A compact summary is looked past too: it is the harness, neither the person nor the agent.
    """
    return turn.speaker in _RETRY_TRIGGERS and not is_slash_command(turn)


def _same_error(a: TranscriptTurn, b: TranscriptTurn) -> bool:
    return (a.tool, a.text, a.result) == (b.tool, b.text, b.result)


def _fold_repeats(turns: list[TranscriptTurn]) -> list[TranscriptTurn]:
    """Each API error folded into the identical one before it, moved to where the newest stands.

    Measured: a full context refuses every notice and peer message that wakes the agent, so its
    errors arrive between those, not back to back. Anything the person or the agent says, a
    tool call, or a harness note such as ``/compact`` ends the run; the next error starts anew.
    """
    folded: list[TranscriptTurn] = []
    open_at = -1
    for turn in turns:
        kept = turn
        if is_api_error(turn):
            if open_at >= 0 and _same_error(folded[open_at], turn):
                earlier = folded.pop(open_at)
                kept = turn.model_copy(update={"times": earlier.times + turn.times})
            open_at = len(folded)
        elif not _wakes(turn):
            open_at = -1
        folded.append(kept)
    return folded


def ends_refused(turns: Sequence[TranscriptTurn]) -> TranscriptTurn | None:
    """The API error the conversation ends on, past what only woke it to retry; else ``None``.

    The same run ``_fold_repeats`` folds, by the same ``_wakes``: a notice or a peer message set
    off a retry, so it does not end a refusal; a slash command, or anything said, does.
    """
    for turn in reversed(turns):
        if is_api_error(turn):
            return turn
        if not _wakes(turn):
            return None
    return None


class TranscriptAdapter(Protocol):
    """What a harness must answer for its conversation to reach the Chat tab, and what it is."""

    default_roots: ClassVar[tuple[str, ...]]
    harness: ClassVar[Harness]

    def page(self, source: Source, window: Window) -> TranscriptPage:
        """The *window*'s turns or messages, ending at frontier ``before``, oldest first."""
        ...

    def since(self, source: Source, window: Window) -> TranscriptPage:
        """Whatever has been recorded past the *window*'s frontier ``after``, oldest first."""
        ...

    def tally(self, source: Source, *, max_bytes: int = MAX_USAGE_BYTES) -> SessionUsage:
        """What the session has spent, over everything its thread has lived in."""
        ...

    def origin(self, source: Source) -> str:
        """The directory the session was started in, or ``""`` when its store does not say."""
        ...


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


_ADAPTERS: Final[dict[str, TranscriptAdapter]] = {}


def _registry() -> dict[str, TranscriptAdapter]:
    """The adapters, built on first use."""
    if not _ADAPTERS:
        # The adapters import this module, so importing them at the top would be a cycle.
        from herdr_pwa.engine.hermes import HermesAdapter  # noqa: PLC0415
        from herdr_pwa.engine.opencode import OpencodeAdapter  # noqa: PLC0415

        adapters: tuple[TranscriptAdapter, ...] = (
            ClaudeAdapter(),
            OpencodeAdapter(),
            HermesAdapter(),
        )
        _ADAPTERS.update({adapter.harness.kind: adapter for adapter in adapters})
    return _ADAPTERS


def adapter_for(agent_kind: str | None) -> TranscriptAdapter | None:
    """The adapter for *agent_kind*, or ``None`` if there is not one."""
    return _registry().get((agent_kind or "").strip().lower())


def harness_of(agent_kind: str | None) -> Harness | None:
    """What *agent_kind* is and can do, or ``None`` for a kind with no harness here."""
    adapter = adapter_for(agent_kind)
    return adapter.harness if adapter is not None else None


def harness_kinds() -> tuple[str, ...]:
    """Every kind with an adapter here, in the order the new-agent form offers them."""
    return tuple(_registry())


def has_byte_frontier(agent_kind: str | None) -> bool:
    """Whether *agent_kind*'s store is a log grown by appended bytes; ``False`` with no adapter."""
    harness = harness_of(agent_kind)
    return harness is not None and harness.byte_frontier


def history(source: Source, window: Window = NEWEST) -> TranscriptPage:
    """One *window* of *source*'s conversation, oldest first. Never raises."""
    adapter = adapter_for(source.kind)
    if adapter is None:
        return TranscriptPage(state=HistoryState.unsupported)
    if not source.session:
        return TranscriptPage(state=HistoryState.absent)
    try:
        page = (
            adapter.since(source, window)
            if window.after is not None
            else adapter.page(source, window)
        )
    except OSError:
        # Found and then unreadable is not "there is none"; the view must not show a quiet chat.
        return TranscriptPage(state=HistoryState.unreadable)
    return page.model_copy(update={"turns": fold_turns(page.turns)})


def session_origin(agent: Agent, *, root: TranscriptRoots = None) -> str:
    """Where *agent*'s reported session was started, as its own store says, or ``""``."""
    source = Source.of(agent.session, agent_kind=agent.agent, root=root)
    adapter = adapter_for(source.kind)
    if adapter is None or not source.session:
        return ""
    try:
        return adapter.origin(source)
    except OSError:
        return ""


# What it cost: the usage tally


class ModelRef(NamedTuple):
    """One model that answered, and the provider that served it."""

    model: str = ""
    provider: str = ""


class Spend(BaseModel):
    """Answers and what they used, summed; a zero is what the provider did not record."""

    # Distinct assistant messages, not records.
    messages: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cache_creation_tokens: int = 0
    # Measured at 200 000x input on a real session: the figure a naive sum leaves out.
    cache_read_tokens: int = 0
    # Only where the provider records it; a price table kept here would be worse than a gap. Zero is unknown.
    cost: float = 0.0

    @property
    def recorded(self) -> bool:
        """Whether any token was recorded: a provider that writes all zeros said nothing."""
        return any(
            (
                self.input_tokens,
                self.output_tokens,
                self.thinking_tokens,
                self.cache_creation_tokens,
                self.cache_read_tokens,
            )
        )


class ModelSpend(Spend):
    """What one model, as one provider served it, spent over a session's answers."""

    ref: ModelRef


class SpawnedAgent(BaseModel):
    """An agent the session started in the background, by the task id its records name."""

    task: str
    description: str = ""
    started: str = ""
    # When a notice said it was over; a notice that it waits on its own work does not count.
    done: bool = False
    ended: str = ""
    failed: bool = False


class SessionUsage(Spend):
    """What one session has spent, summed over every log its thread has lived in, and the agents
    it started in the background."""

    # The newest message's whole prompt: how full the window is now, not a total.
    context_tokens: int = 0
    models: list[ModelRef] = Field(default_factory=list)
    # Only where the store says which model each answer came from; empty means not split.
    by_model: list[ModelSpend] = Field(default_factory=list)
    logs: int = 0
    # A byte budget stopped the read, so the sums are floors.
    partial: bool = False
    state: HistoryState = HistoryState.absent
    # Prompts the person sent, counted once however many logs a fork copied them into.
    prompts: int = 0
    # When the newest prompt was sent, and when the newest record landed; empty when unknown.
    turn_started: str = ""
    last_at: str = ""
    agents: list[SpawnedAgent] = Field(default_factory=list[SpawnedAgent])


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


def _count(value: int | None) -> int:
    """One usage field as a non-negative int, whatever the provider actually wrote."""
    return value if value is not None and value > 0 else 0


def _add_usage(tally: SessionUsage, usage: _Usage, model: str) -> None:
    """Fold one message's usage into *tally*, in place."""
    details = usage.output_tokens_details
    tally.messages += 1
    tally.input_tokens += _count(usage.input_tokens)
    tally.output_tokens += _count(usage.output_tokens)
    tally.cache_creation_tokens += _count(usage.cache_creation_input_tokens)
    tally.cache_read_tokens += _count(usage.cache_read_input_tokens)
    if details is not None:
        tally.thinking_tokens += _count(details.thinking_tokens)
    if model:
        # Claude Code answers from one provider, so the reference carries none.
        remember(tally, ModelRef(model))


def remember(tally: SessionUsage, ref: ModelRef) -> None:
    """Record *ref* as having answered, once, keeping first-seen order."""
    if ref.model and ref not in tally.models:
        tally.models.append(ref)


def spend_of(tally: SessionUsage, ref: ModelRef) -> ModelSpend | None:
    """*ref*'s row of *tally*'s split, added in first-seen order; ``None`` for no model."""
    if not ref.model:
        return None
    for row in tally.by_model:
        if row.ref == ref:
            return row
    row = ModelSpend(ref=ref)
    tally.by_model.append(row)
    return row


def _prompt_size(usage: _Usage) -> int:
    """Everything that went UP with one message: fresh, cache-written, cache-read."""
    return (
        _count(usage.input_tokens)
        + _count(usage.cache_creation_input_tokens)
        + _count(usage.cache_read_input_tokens)
    )


class LiveTally(BaseModel):
    """A lineage's tally and what is needed to grow it by the lines appended to its newest log."""

    tally: SessionUsage
    seen: set[str] = Field(default_factory=set[str])
    budget: int = 0
    # Where the newest log's last whole line ends.
    end: int = 0


# Measured: a background agent's launch result, and its notices however they were delivered.
_SPAWN_MARKS: Final = (b'"async_launched"', b"<task-notification>", b'"queued_command"')


def _worth(raw: bytes) -> bool:
    """Whether a line can carry usage, a prompt or a spawned agent; nothing else is parsed."""
    if b'"usage"' in raw or b'"promptSource"' in raw or b'"origin"' in raw:
        return True
    return any(mark in raw for mark in _SPAWN_MARKS)


_ASYNC_LAUNCHED: Final = "async_launched"


def _spawned(tally: SessionUsage, task: str) -> SpawnedAgent:
    """*task*'s entry in *tally*, added when this is the first record naming it."""
    for agent in tally.agents:
        if agent.task == task:
            return agent
    agent = SpawnedAgent(task=task)
    tally.agents.append(agent)
    return agent


def _ending(record: ClaudeRecord) -> TaskNotice | PeerMessage | None:
    """What *record* says ended a background agent: its notice, or its hand-back report.

    Measured: an agent that hands back while its own work runs is told "waiting", and once
    handed back its run is over, so no "finished" ever follows.
    """
    found = _delivered(record)
    content = record.message.content if record.message is not None else None
    if found is None and record.type == "user" and isinstance(content, str):
        found = injected(record.origin, content, human=_is_human(record))
    if isinstance(found, TaskNotice) and found.kind == "Agent" and not found.waiting:
        return found
    return found if isinstance(found, PeerMessage) and found.report else None


def _launched(record: ClaudeRecord, tally: SessionUsage) -> bool:
    launch = record.tool_use_result
    if launch is None or launch.status != _ASYNC_LAUNCHED or not launch.agent_id:
        return False
    agent = _spawned(tally, launch.agent_id)
    agent.description = launch.description or agent.description
    agent.started = record.timestamp or agent.started
    return True


def _tally_spawned(record: ClaudeRecord, tally: SessionUsage) -> None:
    """Add a background agent's launch or its ending to *tally*, if *record* is one."""
    if _launched(record, tally):
        return
    found = _ending(record)
    if found is None or not found.task:
        return
    agent = _spawned(tally, found.task)
    notice = found if isinstance(found, TaskNotice) else None
    if notice is not None and not agent.description:
        agent.description = notice.detail
    at = record.timestamp or ""
    # Logs are read newest first, then their ancestors: the latest ending stands.
    if not agent.done or at > agent.ended:
        agent.done, agent.ended = True, at
        agent.failed = notice is not None and notice.failed


def _tally_log(path: Path, live: LiveTally, *, newest: bool, start: int = 0) -> int:
    """Add *path*'s whole lines from *start* to *live*; return where the last of them ends."""
    end = start
    with path.open("rb") as handle:
        handle.seek(start)
        for raw in handle:
            if not raw.endswith(b"\n"):
                # A line still being written is read once it is whole.
                break
            live.budget -= len(raw)
            if live.budget < 0:
                live.tally.partial = True
                live.budget = 0
                return end
            end += len(raw)
            if _worth(raw):
                _tally_line(raw, live.tally, live.seen, newest=newest)
    return end


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


def _tally_line(raw: bytes, tally: SessionUsage, seen: set[str], *, newest: bool) -> None:
    """Add one record to *tally*: its message unless already counted, or the prompt it is."""
    record = _record(raw)
    if newest and record.timestamp:
        tally.last_at = record.timestamp
    _tally_spawned(record, tally)
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
        live.tally.logs += 1
        end = _tally_log(log, live, newest=index == 0)
        if index == 0:
            live.end = end
        if live.tally.partial:
            break
    live.tally.state = HistoryState.ok if live.tally.messages else HistoryState.empty
    return live


def grow_claude(live: LiveTally, path: Path) -> None:
    """Add what was appended to the newest log since *live* last read it."""
    if live.tally.partial:
        return
    live.end = _tally_log(path, live, newest=True, start=live.end)
    live.tally.state = HistoryState.ok if live.tally.messages else HistoryState.empty


def _tally_lineage(path: Path, *, max_bytes: int) -> SessionUsage:
    """Sum *path* and every log its thread was copied out of."""
    return tally_claude(path, max_bytes=max_bytes).tally


def usage(source: Source, *, max_bytes: int = MAX_USAGE_BYTES) -> SessionUsage:
    """What *source*'s session has spent so far, across its whole lineage. Never raises."""
    adapter = adapter_for(source.kind)
    if adapter is None:
        return SessionUsage(state=HistoryState.unsupported)
    if not source.session:
        return SessionUsage(state=HistoryState.absent)
    try:
        return adapter.tally(source, max_bytes=max_bytes)
    except OSError:
        return SessionUsage(state=HistoryState.unreadable)
