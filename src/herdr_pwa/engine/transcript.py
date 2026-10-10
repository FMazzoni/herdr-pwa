"""The provider transcript: the only source that is a conversation, not a screen."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Iterator, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Final, NamedTuple, Protocol

from pydantic import BaseModel, Field

from herdr_pwa.diff import FileDiff, tool_diff
from herdr_pwa.engine.model import SessionId

__all__ = [
    "AGENT_KINDS",
    "CLAUDE_TRANSCRIPT_ROOT",
    "MAX_NOTE_CHARS",
    "MAX_PAGE_BYTES",
    "MAX_PROSE_CHARS",
    "MAX_RESULT_CHARS",
    "MAX_SUMMARY_CHARS",
    "MAX_USAGE_BYTES",
    "HistoryState",
    "ModelRef",
    "OneRoot",
    "SessionUsage",
    "Speaker",
    "TranscriptAdapter",
    "TranscriptPage",
    "TranscriptRoots",
    "TranscriptTurn",
    "adapter_for",
    "fold_tool_results",
    "history",
    "locate",
    "read_page",
    "read_since",
    "usage",
]

CLAUDE_TRANSCRIPT_ROOT: Final = "~/.claude/projects"
"""Where Claude Code keeps its per-project transcript directories.
"""

AGENT_KINDS: Final = ("claude", "opencode", "hermes")
"""Every harness with an adapter, and the prefixes a root entry may carry.

@doc: docs/modules/engine/transcript/AGENT_KINDS.md
"""

type OneRoot = str | os.PathLike[str]
"""A single transcript root, as a path or as the string an env var carries.
"""

type TranscriptRoots = OneRoot | Sequence[OneRoot] | None
"""One root, several searched IN ORDER, or the provider's default.
"""

MAX_PAGE_BYTES: Final = 4 * 1024 * 1024
"""Never read more than this from disk to answer one page.
"""

MAX_PROSE_CHARS: Final = 12000
"""**Prose** -- the model's text, and a person's own prompt.
"""

MAX_RESULT_CHARS: Final = 2000
"""**A tool result** -- a gist, deliberately.
"""

MAX_NOTE_CHARS: Final = 2000
"""**Injected context** -- an orchestrator notification, a slash command's expansion.
"""

MAX_SUMMARY_CHARS: Final = 160
"""How long a collapsed one-liner may be.
"""

MAX_USAGE_BYTES: Final = 64 * 1024 * 1024
"""Never read more than this, across the whole lineage, to answer one tally.

@doc: docs/modules/engine/transcript/MAX_USAGE_BYTES.md
"""

_CHUNK: Final = 256 * 1024
"""How much of the file one backward step reads.
"""

_HEAD_BYTES: Final = 64 * 1024
"""How much of a sibling's HEAD is read to learn what it was copied from.
"""

_TAIL_BYTES: Final = 64 * 1024
"""How much of a log's TAIL is read to learn when it last said anything.
"""

_ANOTHER_FILE: Final = 1 << 62
"""A cursor past the end of any file, which `read_since` answers with a reset.

@doc: docs/modules/engine/transcript/_ANOTHER_FILE.md
"""

_MAX_HOPS: Final = 8
"""How long a chain of copies is followed before it is treated as a loop.
"""


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
    """A prompt a person actually typed or queued."""

    agent = "agent"
    """Prose or thinking from the model."""

    tool = "tool"
    """A tool call the agent made, and its result."""

    system = "system"
    """Injected context that is neither: notifications, slash commands, meta."""


class TranscriptTurn(BaseModel):
    """One renderable line of conversation."""

    uuid: str = Field(description="The provider's record id. Stable, unique.")
    speaker: Speaker
    text: str
    ts: str = Field(default="", description="ISO 8601 as the provider wrote it.")
    tool: str = Field(default="", description="Tool name, when speaker is tool.")
    truncated: bool = Field(
        default=False, description="text was cut at its kind's cap."
    )
    call: str = Field(
        default="",
        description=(
            "The provider's tool_use id. Carried by a call AND by its result, "
            "which is the only thing that pairs the two -- they are separate "
            "records and there may be other records between them."
        ),
    )
    result: str = Field(
        default="", description="A folded-in tool result body (see fold_tool_results)."
    )
    failed: bool = Field(
        default=False, description="The provider marked the result is_error."
    )
    result_truncated: bool = Field(default=False, description="result was cut.")
    diff: FileDiff | None = Field(
        default=None,
        description=(
            "WHAT THE CALL CHANGED, when its arguments carry a change: an Edit's "
            "two strings, a Write's contents. It is not in `text` and must not "
            "be -- `text` is the one-line gist and the gist stays the PATH "
            "(after Collie's summarizeToolInput). This is the "
            "expanded body's, computed by herdr_pwa.diff and drawn by "
            "templates/diff.html, which owns every tag it can emit."
        ),
    )


class TranscriptPage(BaseModel):
    """One page of turns, oldest first, plus what to ask for next."""

    turns: list[TranscriptTurn] = Field(default_factory=list)
    cursor: int = 0
    size: int = 0
    has_more: bool = False
    end: int = Field(
        default=0,
        description=(
            "Byte offset just past the newest line this page took in. Handing it "
            "back as ``after`` reads what has been APPENDED since -- which is "
            "what makes the chat append rather than re-render."
        ),
    )
    reset: bool = Field(
        default=False,
        description=(
            "The file is smaller than the cursor held against it, so the "
            "append-only assumption broke (a rewrite, a compaction, a different "
            "file at the same path). The held cursor means nothing and the view "
            "must re-read the tail rather than splice the two together."
        ),
    )
    moved: bool = Field(
        default=False,
        description=(
            "The reset above happened because the cursor named ANOTHER FILE, not "
            "because this one was rewritten. Both drop the cursor; they "
            "are different sentences on the screen, and the view must not say a "
            "record was rewritten when the conversation simply moved."
        ),
    )
    state: HistoryState = Field(
        default=HistoryState.absent,
        description=(
            "Why this page is what it is. The default is ``absent`` because that "
            "is the only thing true of a page nobody read."
        ),
    )
    log: str = Field(
        default="",
        description=(
            "WHICH FILE this page was actually read out of, as its stem -- which "
            "is not the session herdr named once a fork has been followed "
            "(after a fork). Two readers need it: the cursor, which is a promise about "
            "one file and must not be honoured against another, and the stall "
            "watch, whose byte counts mean nothing across a change of file."
        ),
    )


# ---------------------------------------------------------------------------
# Locating the file
# ---------------------------------------------------------------------------


def _for_kind(entry: str, kind: str) -> str:
    """*entry* as it applies to *kind*: unqualified, qualified for it, or empty."""
    # @doc: docs/modules/engine/transcript.md
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
    # @doc: docs/modules/engine/transcript.md
    if root is None:
        given: Sequence[OneRoot] = list(default)
    elif isinstance(root, (str, os.PathLike)):
        given = [root]
    else:
        given = list(root)
    found: list[Path] = []
    for entry in given:
        # An empty entry resolves relative to the process cwd, which is
        # nobody's transcript directory. Dropped, never searched.
        text = _for_kind(str(entry).strip(), kind)
        if not text.strip():
            continue
        path = Path(text.strip()).expanduser()
        if path not in found:
            found.append(path)
    return found


def _contained(path: Path, base: Path) -> Path | None:
    """*path* resolved, then re-checked to still be a file inside *base*."""
    # Resolve, then re-check containment: a symlink satisfies any comparison of
    # The strings we were handed. Callers `continue`, never `return`.
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


def _head_records(window: bytes) -> Iterator[dict[str, Any]]:
    """Every record *window* holds WHOLE, in order.

    @doc: docs/modules/engine/transcript/_head_records.md
    """
    # The trailing fragment is dropped: past the budget it is half a record,
    # and at EOF it is the empty string after the final newline.
    for raw in window.split(b"\n")[:-1]:
        yield _loads(raw)


def _head_record(path: Path) -> dict[str, Any]:
    """The first whole record in *path* that carries a ``sessionId``."""
    for record in _head_records(_head_window(path)):
        if isinstance(record.get("sessionId"), str):
            return record
    return {}


def _loads(raw: bytes) -> dict[str, Any]:
    """One JSONL line as a dict, or an empty one for anything that is not."""
    if not raw.strip():
        return {}
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    return record if isinstance(record, dict) else {}


def _forked_from(record: dict[str, Any]) -> str:
    """The declared parent on *record*, as logs up to 2.1.214 spell it."""
    forked = record.get("forkedFrom")
    parent = forked.get("sessionId") if isinstance(forked, dict) else None
    return parent if isinstance(parent, str) else ""


def _stamped_parent(window: bytes, own: str) -> str:
    """The first foreign ``session_id`` in *window* -- a BACKGROUND session's stamp.

    @doc: docs/modules/engine/transcript/_stamped_parent.md
    """
    # TWO STEPS, the way `_background_handoff` does it: the byte
    # substrings are a cheap PREFILTER, and `sessionKind` is confirmed on a
    # parsed record. @doc: docs/modules/engine/transcript/_stamped_parent.md
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
    # Only reached when the confirming record came BEFORE any stamp, so the
    # first loop had nothing to carry: this re-reads the same parsed head.
    for record in _head_records(window):
        value = record.get("session_id")
        if isinstance(value, str) and value and value != own:
            return value
    return ""


def _copied_from(path: Path) -> str:
    """The session *path*'s thread was COPIED OUT OF, per its own head window.

    @doc: docs/modules/engine/transcript/_copied_from.md
    """
    window = _head_window(path)
    head = {}
    for record in _head_records(window):
        if isinstance(record.get("sessionId"), str):
            head = record
            break
    return _forked_from(head) or _stamped_parent(window, str(head.get("sessionId", "")))


def _is_compact(record: dict[str, Any]) -> bool:
    """Whether *record* is a compaction boundary, however the CLI spelled it."""
    return (
        isinstance(record.get("compactMetadata"), dict)
        or record.get("isCompactSummary") is True
        or record.get("subtype") == "compact_boundary"
    )


def _background_handoff(path: Path) -> bool:
    """Whether *path*'s head declares a compaction AND a background session.

    @doc: docs/modules/engine/transcript/_background_handoff.md
    """
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
        onward: list[str] = []
        for parent in frontier:
            for child in edges.get(parent, ()):
                if child.stem in seen:
                    continue
                seen.add(child.stem)
                found.append(child)
                onward.append(child.stem)
        if not onward:
            return found
        frontier = onward
    return found


def _follow(named: Path, session: str, base: Path) -> Path:
    """*named*, or the copy the conversation continued in. Never LESS history.

    @doc: docs/modules/engine/transcript/_follow.md
    """
    # @doc: docs/modules/engine/transcript.md
    newest = _last_timestamp(named)
    try:
        floor = named.stat().st_size
    except OSError:
        return named
    if not newest:
        return named
    best = named
    for candidate in _descendants(session, _copies(named.parent)):
        # Containment is re-checked per candidate, against the SAME root the
        # named file was resolved through -- never a union of roots.
        resolved = _contained(candidate, base)
        if resolved is None:
            continue
        try:
            if resolved.stat().st_size < floor and not _background_handoff(resolved):
                continue
        except OSError:
            continue
        stamp = _last_timestamp(resolved)
        if stamp > newest:
            best, newest = resolved, stamp
    return best


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


# ---------------------------------------------------------------------------
# Reading it, backwards, bounded
# ---------------------------------------------------------------------------


def _lines_before(
    path: Path, before: int, budget: int
) -> tuple[list[tuple[int, bytes]], int]:
    """Whole lines ending at byte *before*, oldest first, each with its offset."""
    start = max(0, before - budget)
    with path.open("rb") as handle:
        handle.seek(start)
        window = handle.read(before - start)
    if start > 0:
        head, sep, rest = window.partition(b"\n")
        if not sep:
            # @doc: docs/modules/engine/transcript.md
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
    # @doc: docs/modules/engine/transcript.md
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
    # No newline anywhere means no whole line and nothing delivered. Past the
    # budget, one record is longer than we will scan back over: leave it.
    return 0 if frontier == 0 else end


def _parse_line(
    raw: bytes, parse: Callable[[dict[str, Any]], TranscriptTurn | None]
) -> TranscriptTurn | None:
    """One JSONL line as a turn, or ``None`` for anything that is not one."""
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return parse(record) if isinstance(record, dict) else None


def _take_backwards(
    lines: list[tuple[int, bytes]],
    parse: Callable[[dict[str, Any]], TranscriptTurn | None],
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


def read_page(
    path: Path,
    *,
    limit: int = 40,
    before: int | None = None,
    max_bytes: int = MAX_PAGE_BYTES,
    parse: Callable[[dict[str, Any]], TranscriptTurn | None] | None = None,
) -> TranscriptPage:
    """Up to *limit* turns ending at byte *before*, oldest first."""
    parse = parse or _claude_turn
    try:
        size = path.stat().st_size
    except OSError:
        return TranscriptPage(state=HistoryState.unreadable)
    end = _complete_end(path, size if before is None else max(0, min(before, size)))
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
        # @doc: docs/modules/engine/transcript.md
        cursor = start if taken is None else taken
    if frontier == 0 and not hit_limit:
        # @doc: docs/modules/engine/transcript.md
        cursor = 0
    turns.reverse()
    return TranscriptPage(
        turns=turns,
        cursor=cursor,
        size=size,
        has_more=cursor > 0,
        # @doc: docs/modules/engine/transcript.md
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
    parse: Callable[[dict[str, Any]], TranscriptTurn | None] | None = None,
) -> TranscriptPage:
    """Whatever has been APPENDED past byte *after*, oldest first."""
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
        # Not one whole line yet: either the record is still being written or it
        # is longer than the budget. Either way nothing here can be parsed, and
        # the cursor must NOT move past bytes that were never delivered.
        return TranscriptPage(
            size=size,
            end=start,
            has_more=size > start,
            state=HistoryState.empty,
            log=path.stem,
        )
    turns: list[TranscriptTurn] = []
    # @doc: docs/modules/engine/transcript.md
    end = start
    offset = start
    for raw in window[:tail].split(b"\n"):
        offset += len(raw) + 1
        end = offset
        if not raw.strip():
            continue
        turn = _parse_line(raw, parse)
        if turn is not None:
            turns.append(turn)
        if len(turns) >= limit:
            break
    return TranscriptPage(
        turns=turns,
        cursor=start,
        size=size,
        end=end,
        has_more=end < size,
        state=HistoryState.ok if turns else HistoryState.empty,
        log=path.stem,
    )


# ---------------------------------------------------------------------------
# The Claude Code adapter
# ---------------------------------------------------------------------------

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
"""Record types that carry no conversation.
"""

_HUMAN_SOURCES: Final = frozenset({"typed", "queued", "pasted"})
"""``promptSource`` values that mean a person actually said this.
"""


def _is_human(record: dict[str, Any]) -> bool:
    source = record.get("promptSource")
    if isinstance(source, dict):
        source = source.get("kind")
    if isinstance(source, str) and source in _HUMAN_SOURCES:
        return True
    origin = record.get("origin")
    return isinstance(origin, dict) and origin.get("kind") == "human"


def _clip(text: str, limit: int) -> tuple[str, bool]:
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
"""Which of a tool's arguments IDENTIFIES the call, in preference order.
"""


def _one_line(value: object, limit: int = MAX_SUMMARY_CHARS) -> tuple[str, bool]:
    """*value* as a single collapsed line, clipped, with whether it was clipped."""
    if not isinstance(value, str):
        return "", False
    return _clip(" ".join(value.split()), limit)


def _tool_summary(params: object) -> tuple[str, bool]:
    """The one identifying argument of a tool call (see `_TOOL_ARGUMENT_KEYS`)."""
    if not isinstance(params, dict):
        return "", False
    for key in _TOOL_ARGUMENT_KEYS:
        if key in params:
            line, cut = _one_line(params.get(key))
            if line:
                return line, cut
    return "", False


def _blocks(content: object) -> Iterator[dict[str, Any]]:
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                yield block


def _user_record(record: dict[str, Any], uuid: str, ts: str) -> TranscriptTurn | None:
    """A ``role: "user"`` record, which is where the whole trap lives."""
    content = record.get("message", {}).get("content")
    if isinstance(content, str):
        if record.get("isMeta"):
            return None
        # The cap follows the SPEAKER, so it is decided before the clip: a
        # person's own prompt is prose and gets the prose cap, and injected
        # machinery gets the note cap, because the view shows three lines of it.
        human = _is_human(record)
        text, cut = _clip(content, MAX_PROSE_CHARS if human else MAX_NOTE_CHARS)
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


def _user_block(
    block: dict[str, Any], uuid: str, ts: str, *, meta: bool
) -> TranscriptTurn | None:
    """One content block of a user record, or ``None`` if it is not conversation."""
    kind = block.get("type")
    if kind == "tool_result":
        body = block.get("content")
        text, cut = _clip(
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
        text, cut = _clip(str(block.get("text", "")), MAX_NOTE_CHARS)
        if text:
            return TranscriptTurn(
                uuid=uuid, speaker=Speaker.system, text=text, ts=ts, truncated=cut
            )
    return None


def _assistant_record(
    record: dict[str, Any], uuid: str, ts: str
) -> TranscriptTurn | None:
    """An assistant record: prose, or one collapsed tool call."""
    for block in _blocks(record.get("message", {}).get("content")):
        btype = block.get("type")
        if btype == "text":
            text, cut = _clip(str(block.get("text", "")), MAX_PROSE_CHARS)
            if not text:
                return None
            return TranscriptTurn(
                uuid=uuid, speaker=Speaker.agent, text=text, ts=ts, truncated=cut
            )
        if btype == "tool_use":
            params = block.get("input")
            text, cut = _tool_summary(params)
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
            # Kept out of the chat: 393 of them in the measured file, with
            # an empty ``thinking`` string and a signature blob. Nothing to
            # render, and a bubble per record would be 393 empty bubbles.
            return None
    return None


def _claude_turn(record: dict[str, Any]) -> TranscriptTurn | None:
    """One Claude Code JSONL record as a chat turn, or ``None`` to drop it."""
    kind = record.get("type")
    if kind in _SKIP_TYPES:
        return None
    uuid = record.get("uuid")
    if not isinstance(uuid, str) or not uuid:
        return None
    if not isinstance(record.get("message"), dict):
        return None
    ts = record.get("timestamp") if isinstance(record.get("timestamp"), str) else ""
    if kind == "user":
        return _user_record(record, uuid, ts or "")
    if kind == "assistant":
        return _assistant_record(record, uuid, ts or "")
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
    """What a harness must answer for its conversation to reach the Chat tab.

    @doc: docs/modules/engine/transcript/TranscriptAdapter.md
    """

    default_roots: ClassVar[tuple[str, ...]]
    """Where this provider keeps its history when nothing was configured."""

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
    """Claude Code: one JSONL per session, and the frontier is a BYTE OFFSET.

    @doc: docs/modules/engine/transcript/ClaudeAdapter.md
    """

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
        """Up to *limit* turns ending at byte *before*, oldest first.

        @doc: docs/modules/engine/transcript/ClaudeAdapter.page.md
        """
        path = self._log(session, roots, follow=True)
        if path is None:
            return TranscriptPage(state=HistoryState.absent)
        if expect and expect != path.stem:
            # THE OTHER HALF OF THE CURSOR GUARD: a `before` offset is
            # CLAMPED rather than refused, which is worse than refusing it.
            # @doc: docs/modules/engine/transcript/ClaudeAdapter.page.md
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
        """Whatever has been APPENDED past byte *after*, oldest first.

        @doc: docs/modules/engine/transcript/ClaudeAdapter.since.md
        """
        path = self._log(session, roots, follow=True)
        if path is None:
            return TranscriptPage(state=HistoryState.absent)
        if expect and expect != path.stem:
            # A held cursor is a promise about ONE file, and the lineage moved
            # under this one. Reading at that offset would splice two threads
            # together, so the cursor is dropped and the chat says so.
            page = read_since(path, after=_ANOTHER_FILE, limit=limit)
            return page.model_copy(update={"moved": True})
        return read_since(path, after=after, limit=limit, parse=_claude_turn)

    def tally(
        self, session: str, *, roots: Sequence[Path], max_bytes: int = MAX_USAGE_BYTES
    ) -> SessionUsage:
        """What the session has spent, summed over its whole lineage."""
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
"""Adapter registry, keyed on herdr's ``agent`` field.

@doc: docs/modules/engine/transcript/_ADAPTERS.md
"""


def _registry() -> dict[str, TranscriptAdapter]:
    """The adapters, built on first use."""
    if not _ADAPTERS:
        # Imported here rather than at the top: an adapter speaks this module's
        # vocabulary, so importing one from the top would be a cycle.
        from herdr_pwa.engine.hermes import HermesAdapter
        from herdr_pwa.engine.opencode import OpencodeAdapter

        _ADAPTERS["claude"] = ClaudeAdapter()
        _ADAPTERS["opencode"] = OpencodeAdapter()
        _ADAPTERS["hermes"] = HermesAdapter()
    return _ADAPTERS


def adapter_for(agent_kind: str | None) -> TranscriptAdapter | None:
    """The adapter for *agent_kind*, or ``None`` if there is not one."""
    return _registry().get((agent_kind or "").strip().lower())


def history(
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
        # The history was found and then could not be read: a permission change,
        # a vanished mount, a real I/O error. Distinct from "there is none", and
        # the view says so rather than showing a quiet conversation.
        return TranscriptPage(state=HistoryState.unreadable)
    return page.model_copy(update={"turns": fold_tool_results(page.turns)})


# ---------------------------------------------------------------------------
# What it cost: the usage tally
# ---------------------------------------------------------------------------


class ModelRef(NamedTuple):
    """One model that answered, and the provider that served it.

    @doc: docs/modules/engine/transcript/ModelRef.md
    """

    model: str = ""
    """The model id as its own harness spells it, vendor prefix and all."""

    provider: str = ""
    """Who served it, for the harnesses that can be pointed at more than one."""


class SessionUsage(BaseModel):
    """What one session has spent, summed over every log its thread has lived in."""

    messages: int = Field(
        default=0, description="Distinct assistant MESSAGES, not records."
    )
    input_tokens: int = Field(default=0, description="Fresh prompt, never cached.")
    output_tokens: int = Field(default=0, description="Everything the model generated.")
    thinking_tokens: int = Field(
        default=0, description="The part of output that was reasoning."
    )
    cache_creation_tokens: int = Field(default=0, description="Written into the cache.")
    cache_read_tokens: int = Field(
        default=0,
        description=(
            "Read back OUT of the cache. Measured at 200 000x input on a real "
            "session, so it is the figure a naive sum leaves out and the reason "
            "this model reports four numbers rather than one."
        ),
    )
    context_tokens: int = Field(
        default=0,
        description=(
            "The whole prompt of the NEWEST message -- fresh, cached and "
            "cache-written together. How full the window is now, not a total."
        ),
    )
    models: list[ModelRef] = Field(
        default_factory=list,
        description=(
            "Which models answered, first seen first, each with the provider "
            "that served it -- opencode can be pointed at several."
        ),
    )
    cost: float = Field(
        default=0.0,
        description=(
            "Money, in the currency the provider recorded, and only where the "
            "provider records it. opencode does, per message; Claude "
            "Code does not, and a price table this app would have to keep "
            "current is a worse answer than a gap. Zero means UNKNOWN."
        ),
    )
    logs: int = Field(default=0, description="How many files the tally covers.")
    partial: bool = Field(
        default=False,
        description="A byte budget stopped the read; the sums are floors.",
    )
    state: HistoryState = Field(default=HistoryState.absent)


_SYNTHETIC_MODEL: Final = "<synthetic>"
"""The model name Claude Code writes on a message no API call produced.

@doc: docs/modules/engine/transcript/_SYNTHETIC_MODEL.md
"""


def _usage_of(record: dict[str, Any]) -> tuple[str, dict[str, Any], str] | None:
    """An assistant record's ``(message id, usage, model)``, or ``None``."""
    if record.get("type") != "assistant":
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    identifier = message.get("id")
    if not isinstance(usage, dict) or not isinstance(identifier, str) or not identifier:
        return None
    model = message.get("model") if isinstance(message.get("model"), str) else ""
    if model == _SYNTHETIC_MODEL:
        return None
    return identifier, usage, str(model)


def _count(usage: dict[str, Any], key: str) -> int:
    """One usage field as a non-negative int, whatever the provider actually wrote."""
    value = usage.get(key)
    return value if isinstance(value, int) and value > 0 else 0


def _add_usage(tally: SessionUsage, usage: dict[str, Any], model: str) -> None:
    """Fold one message's usage into *tally*, in place."""
    details = usage.get("output_tokens_details")
    tally.messages += 1
    tally.input_tokens += _count(usage, "input_tokens")
    tally.output_tokens += _count(usage, "output_tokens")
    tally.cache_creation_tokens += _count(usage, "cache_creation_input_tokens")
    tally.cache_read_tokens += _count(usage, "cache_read_input_tokens")
    if isinstance(details, dict):
        tally.thinking_tokens += _count(details, "thinking_tokens")
    if model:
        # Claude Code answers from one provider, so the reference carries none.
        _remember(tally, ModelRef(model))


def _remember(tally: SessionUsage, ref: ModelRef) -> None:
    """Record *ref* as having answered, once, keeping first-seen order."""
    if ref.model and ref not in tally.models:
        tally.models.append(ref)


def _prompt_size(usage: dict[str, Any]) -> int:
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
    # A message is written as several RECORDS, all carrying the same id and the
    # same usage byte for byte, so summing records nearly doubles the answer:
    # the id set is the whole correctness of this function.
    # @doc: docs/modules/engine/transcript/usage.md
    with path.open("rb") as handle:
        for raw in handle:
            budget -= len(raw)
            if budget < 0:
                tally.partial = True
                return 0
            if b'"usage"' not in raw:
                continue
            found = _usage_of(_loads(raw))
            if found is None:
                continue
            identifier, usage, model = found
            if newest:
                # Forward through the newest log, so the last one to land here
                # is the newest message there is: that is the live window.
                tally.context_tokens = _prompt_size(usage)
            if identifier in seen:
                continue
            seen.add(identifier)
            _add_usage(tally, usage, model)
    return budget


def _ancestors(path: Path) -> list[Path]:
    """Every log *path*'s thread was copied out of, newest first."""
    # @doc: docs/modules/engine/transcript/_ancestors.md
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
    # @doc: docs/modules/engine/transcript/usage.md
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
    # @doc: docs/modules/engine/transcript/usage.md
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
