"""Hermes Agent's SQLite store, read as one conversation."""

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, ClassVar, Final, override

from pydantic import BaseModel, ConfigDict, Json, TypeAdapter, ValidationError

from herdr_pwa.diff import tool_diff
from herdr_pwa.engine.store import Row, SessionStore, consult
from herdr_pwa.engine.transcript import (
    MAX_PAGE_BYTES,
    MAX_PROSE_CHARS,
    MAX_RESULT_CHARS,
    MAX_USAGE_BYTES,
    Gather,
    HistoryState,
    ModelRef,
    SessionUsage,
    Speaker,
    ToolInput,
    TranscriptAdapter,
    TranscriptPage,
    TranscriptTurn,
    clip,
    remember,
    tool_summary,
)
from herdr_pwa.validation import Text, or_default

HERMES_ROOT: Final = "~/.hermes"

HERMES_DB: Final = "state.db"

_ROW_BYTES: Final = 512


def _stamp(value: object) -> str:
    """Hermes's epoch seconds as ISO 8601, or empty when it wrote nothing usable."""
    if not isinstance(value, int | float) or value <= 0:
        return ""
    try:
        return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
    except (OSError, OverflowError, ValueError):
        return ""


class _Function(BaseModel):
    """What one tool call invoked, and with what."""

    model_config = ConfigDict(extra="ignore")

    name: Text = None
    # Hermes writes the arguments as JSON text inside the JSON; an older row wrote the object.
    arguments: Annotated[ToolInput | Json[ToolInput] | None, or_default(None)] = None


class _ToolCall(BaseModel):
    """One entry of a row's ``tool_calls`` array."""

    model_config = ConfigDict(extra="ignore")

    id: Text = None
    call_id: Text = None
    function: Annotated[_Function | None, or_default(None)] = None


_CALLS: Final = TypeAdapter(list[Annotated[_ToolCall | None, or_default(None)]])


def _calls(blob: object) -> list[_ToolCall]:
    """The ``tool_calls`` column as a list, or empty for anything unreadable."""
    if not isinstance(blob, str) or not blob.strip():
        return []
    try:
        found = _CALLS.validate_json(blob)
    except ValidationError:
        return []
    return [call for call in found if call is not None]


def _as_int(value: object) -> int:
    """``int(value)`` for the column types SQLite hands back."""
    if isinstance(value, int | float | str | bytes):
        return int(value)
    raise TypeError(f"not a number: {type(value).__name__}")


def _stream(db: SessionStore) -> list[int]:
    """Every message id for this session, oldest first."""
    return [_as_int(row[0]) for row in db.rows(_STREAM_SQL, db.session)]


def _rows(db: SessionStore, ids: Sequence[int]) -> dict[int, Row]:
    """The full record behind each of *ids*, keyed by id."""
    if not ids:
        return {}
    holes = ",".join("?" * len(ids))
    # SQLite cannot bind a variable-length IN list; `holes` is only `?` placeholders.
    columns = "id, role, content, tool_call_id, tool_calls, tool_name, timestamp"
    sql = f"select {columns} from messages where session_id = ? and id in ({holes})"  # noqa: S608
    return {_as_int(row[0]): row for row in db.rows(sql, db.session, *ids)}


def _info(db: SessionStore) -> tuple[str, str]:
    """``(title, model)`` off the session's own row, empty when it has none."""
    found = db.first(_INFO_SQL, db.session)
    if found is None:
        return "", ""
    title, model = found
    return str(title or ""), str(model or "")


def _origin(db: SessionStore) -> str:
    """The ``cwd`` Hermes recorded on the session's own row."""
    found = db.first(_ORIGIN_SQL, db.session)
    return str(found[0] or "").strip() if found else ""


def _clock(db: SessionStore) -> Row:
    """``(prompts, newest prompt, newest message)`` as the store holds them."""
    return db.first(_CLOCK_SQL, db.session) or (0, None, None)


_STREAM_SQL: Final = "select id from messages where session_id = ? and active = 1 order by id"

_USAGE_SQL: Final = (
    "select model, billing_provider, api_call_count, input_tokens,"
    " output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens,"
    " actual_cost_usd, estimated_cost_usd, cost_status"
    " from session_model_usage where session_id = ?"
)

_CLOCK_SQL: Final = (
    "select count(case when role = 'user' then 1 end),"
    " max(case when role = 'user' then timestamp end), max(timestamp)"
    " from messages where session_id = ? and active = 1"
)

_ORIGIN_SQL: Final = "select cwd from sessions where id = ? limit 1"

_INFO_SQL: Final = "select title, model from sessions where id = ? limit 1"

_COST_UNKNOWN: Final = frozenset({"", "none", "unknown"})


def _turn(row: Row) -> TranscriptTurn | None:
    """One ``messages`` row as a chat turn, or ``None`` to drop it."""
    identifier, role, content, call_id, calls, tool_name, stamp = row
    uuid = str(identifier)
    ts = _stamp(stamp)
    text = content if isinstance(content, str) else ""
    if role == "user":
        body, cut = clip(text, MAX_PROSE_CHARS)
        if not body:
            return None
        return TranscriptTurn(uuid=uuid, speaker=Speaker.human, text=body, ts=ts, truncated=cut)
    if role == "tool":
        body, cut = clip(text, MAX_RESULT_CHARS)
        return TranscriptTurn(
            uuid=uuid,
            speaker=Speaker.tool,
            text=body,
            ts=ts,
            tool="result",
            truncated=cut,
            call=str(call_id or ""),
        )
    if role != "assistant":
        return None
    body, cut = clip(text, MAX_PROSE_CHARS)
    if body:
        return TranscriptTurn(uuid=uuid, speaker=Speaker.agent, text=body, ts=ts, truncated=cut)
    return _call_turn(_calls(calls), uuid=uuid, ts=ts, tool_name=tool_name)


def _call_turn(
    calls: list[_ToolCall], *, uuid: str, ts: str, tool_name: object
) -> TranscriptTurn | None:
    """The FIRST tool call of an assistant row, collapsed the way Claude's is."""
    if not calls:
        return None
    first = calls[0]
    function = first.function or _Function()
    summary, cut = tool_summary(function.arguments)
    return TranscriptTurn(
        uuid=uuid,
        speaker=Speaker.tool,
        text=summary,
        ts=ts,
        tool=str(function.name or tool_name or ""),
        truncated=cut,
        call=first.id or first.call_id or "",
        diff=tool_diff(function.arguments),
    )


def _charged(row: Row | None) -> int:
    """What one row costs the page budget: its text, plus a floor for the rest."""
    if row is None:
        return _ROW_BYTES
    text = sum(len(value) for value in row if isinstance(value, str))
    return text + _ROW_BYTES


def _page(
    db: SessionStore, *, gather: Gather, before: int | None, max_bytes: int
) -> TranscriptPage:
    """Turns until *gather* is full, ending at frontier *before*, oldest first."""
    stream = _stream(db)
    size = len(stream)
    end = size if before is None else max(0, min(before, size))
    limit = gather.limit
    rows = _rows(db, stream[max(0, end - limit) : end])
    positions = range(end, max(0, end - limit), -1)
    turns, last = _take(stream, rows, positions, gather=gather, max_bytes=max_bytes)
    cursor = end if last is None else last - 1
    turns.reverse()
    return TranscriptPage(
        turns=turns,
        cursor=cursor,
        size=size,
        has_more=cursor > 0,
        end=end,
        state=HistoryState.ok if turns else HistoryState.empty,
    )


def _since(db: SessionStore, *, after: int, limit: int, max_bytes: int) -> TranscriptPage:
    """Whatever the store has grown past frontier *after*, oldest first."""
    stream = _stream(db)
    size = len(stream)
    start = max(0, after)
    if start > size:
        return TranscriptPage(size=size, end=size, reset=True, state=HistoryState.empty)
    rows = _rows(db, stream[start : start + limit])
    positions = range(start + 1, min(size, start + limit) + 1)
    turns, last = _take(stream, rows, positions, gather=Gather(limit), max_bytes=max_bytes)
    end = start if last is None else last
    return TranscriptPage(
        turns=turns,
        cursor=start,
        size=size,
        end=end,
        has_more=end < size,
        state=HistoryState.ok if turns else HistoryState.empty,
    )


def _take(
    stream: list[int], rows: dict[int, Row], positions: range, *, gather: Gather, max_bytes: int
) -> tuple[list[TranscriptTurn], int | None]:
    """Turns at *positions* in walk order, and the last position the budget paid for."""
    spent = 0
    last: int | None = None
    for position in positions:
        row = rows.get(stream[position - 1])
        spent += _charged(row)
        if spent > max_bytes:
            break
        last = position
        if gather.take(_row_turn(row), position - 1):
            break
    return gather.turns, last


def _row_turn(row: Row | None) -> TranscriptTurn | None:
    """A row as a turn; a missing row is no turn."""
    return None if row is None else _turn(row)


def _tally(db: SessionStore) -> SessionUsage:
    """What *session* has spent, off the per-model rollup Hermes already keeps."""
    tally = SessionUsage(state=HistoryState.ok, logs=1)
    for row in db.rows(_USAGE_SQL, db.session):
        model, provider, calls, prompt, produced, cached, written, thought = row[:8]
        remember(tally, ModelRef(str(model or ""), str(provider or "")))
        tally.messages += _as_int(calls or 0)
        tally.input_tokens += _as_int(prompt or 0)
        tally.output_tokens += _as_int(produced or 0)
        tally.cache_read_tokens += _as_int(cached or 0)
        tally.cache_creation_tokens += _as_int(written or 0)
        tally.thinking_tokens += _as_int(thought or 0)
        tally.cost += _money(*row[8:11])
    prompts, asked, latest = _clock(db)
    tally.prompts = _as_int(prompts or 0)
    tally.turn_started = _stamp(asked)
    tally.last_at = _stamp(latest)
    return tally


def _money(actual: object, estimated: object, status: object) -> float:
    """What one model row cost, or 0.0 -- which this app's own field calls UNKNOWN."""
    if str(status or "").strip().lower() in _COST_UNKNOWN:
        return 0.0
    for value in (actual, estimated):
        if isinstance(value, int | float) and value > 0:
            return float(value)
    return 0.0


def _open(roots: Sequence[Path], session: str) -> SessionStore | None:
    """The first root that holds *session*, or ``None``."""
    for base in roots:
        found = SessionStore.open(Path(base) / HERMES_DB, table="sessions", session=session)
        if found is not None:
            return found
    return None


class HermesAdapter(TranscriptAdapter):
    """Hermes's history: one SQLite store, paged by POSITION not byte."""

    default_roots: ClassVar[tuple[str, ...]] = (HERMES_ROOT,)

    @override
    def page(
        self,
        session: str,
        *,
        roots: Sequence[Path],
        limit: int = 40,
        before: int | None = None,
        max_bytes: int = MAX_PAGE_BYTES,
        expect: str = "",
        messages: int = 0,
    ) -> TranscriptPage:
        """Up to *limit* turns, or *messages* messages, ending at *before*, oldest first."""
        # `expect` is ignored: one file holds every session, so it cannot move under a cursor.
        gather = Gather(limit, messages)
        return consult(
            lambda: _open(roots, session),
            lambda db: _page(db, gather=gather, before=before, max_bytes=max_bytes),
            absent=TranscriptPage(state=HistoryState.absent),
            locked=TranscriptPage(state=HistoryState.unreadable),
        )

    @override
    def since(
        self,
        session: str,
        *,
        roots: Sequence[Path],
        after: int,
        limit: int = 40,
        max_bytes: int = MAX_PAGE_BYTES,
        expect: str = "",
    ) -> TranscriptPage:
        """Whatever the store has grown past frontier *after*, oldest first."""
        return consult(
            lambda: _open(roots, session),
            lambda db: _since(db, after=after, limit=limit, max_bytes=max_bytes),
            absent=TranscriptPage(state=HistoryState.absent),
            locked=TranscriptPage(state=HistoryState.unreadable),
        )

    def origin(self, session: str, *, roots: Sequence[Path]) -> str:
        """The directory Hermes recorded *session* as started in, or ``""``."""
        return consult(lambda: _open(roots, session), _origin, absent="", locked="")

    def info(self, session: str, *, roots: Sequence[Path] = ()) -> tuple[str, str]:
        """``(title, model)`` off *session*'s own row. Never raises."""
        where = roots or [Path(root) for root in self.default_roots]
        return consult(lambda: _open(where, session), _info, absent=("", ""), locked=("", ""))

    @override
    def tally(
        self, session: str, *, roots: Sequence[Path], max_bytes: int = MAX_USAGE_BYTES
    ) -> SessionUsage:
        """What this session has spent. Hermes records no money, so cost stays 0."""
        return consult(
            lambda: _open(roots, session),
            _tally,
            absent=SessionUsage(state=HistoryState.absent),
            locked=SessionUsage(state=HistoryState.unreadable),
        )
