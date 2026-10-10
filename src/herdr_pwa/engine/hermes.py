"""Hermes Agent's SQLite store, read as one conversation."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Final
from urllib.parse import quote

from herdr_pwa.diff import tool_diff
from herdr_pwa.engine.transcript import (
    MAX_PAGE_BYTES,
    MAX_PROSE_CHARS,
    MAX_RESULT_CHARS,
    MAX_USAGE_BYTES,
    HistoryState,
    ModelRef,
    SessionUsage,
    Speaker,
    TranscriptPage,
    TranscriptTurn,
    _clip,  # noqa: PLC2701 - the shared clip, reused not reinvented
    _remember,  # noqa: PLC2701
    _tool_summary,  # noqa: PLC2701
)


HERMES_ROOT: Final = "~/.hermes"

HERMES_DB: Final = "state.db"

_BUSY_SECONDS: Final = 0.25

_ROW_BYTES: Final = 512


def _readonly(path: Path) -> str:
    """*path* as the only URI this module ever hands SQLite."""
    return f"file:{quote(str(path))}?mode=ro"


def _stamp(value: object) -> str:
    """Hermes's epoch seconds as ISO 8601, or empty when it wrote nothing usable."""
    if not isinstance(value, int | float) or value <= 0:
        return ""
    try:
        return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
    except (OSError, OverflowError, ValueError):
        return ""


def _calls(blob: object) -> list[dict[str, Any]]:
    """The ``tool_calls`` column as a list, or empty for anything unreadable."""
    if not isinstance(blob, str) or not blob.strip():
        return []
    try:
        loaded = json.loads(blob)
    except (TypeError, ValueError):
        return []
    return [item for item in loaded if isinstance(item, dict)] if isinstance(loaded, list) else []


def _arguments(call: dict[str, Any]) -> object:
    """The arguments of one tool call, decoded -- Hermes writes them as JSON TEXT."""
    function = call.get("function")
    raw = function.get("arguments") if isinstance(function, dict) else None
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


class _Db:
    """The store, opened read-only and never locked."""

    def __init__(self, connection: sqlite3.Connection, session: str) -> None:
        self._db = connection
        self._session = session

    @classmethod
    def open(cls, path: Path, session: str) -> _Db | None:
        """*path* if it is a store we recognise and *session* is in it."""
        try:
            resolved = path.expanduser().resolve(strict=True)
        except OSError:
            return None
        try:
            connection = sqlite3.connect(_readonly(resolved), uri=True, timeout=_BUSY_SECONDS)
            connection.execute("select id from sessions limit 0").close()
            held = connection.execute(
                "select 1 from sessions where id = ? limit 1", (session,)
            ).fetchone()
        except (sqlite3.Error, OSError, ValueError):
            return None
        if held is None:
            connection.close()
            return None
        return cls(connection, session)

    def stream(self) -> list[int]:
        """Every message id for this session, oldest first."""
        return [int(row[0]) for row in self._ask(_STREAM_SQL)]

    def rows(self, ids: Sequence[int]) -> dict[int, tuple[Any, ...]]:
        """The full record behind each of *ids*, keyed by id."""
        if not ids:
            return {}
        holes = ",".join("?" * len(ids))
        # S608: `holes` is a run of `?` placeholders and nothing else -- the ids
        # themselves are bound, never interpolated. SQLite has no way to bind a
        # variable-length IN list, so the COUNT has to be built into the text.
        columns = "id, role, content, tool_call_id, tool_calls, tool_name, timestamp"
        sql = f"select {columns} from messages where session_id = ? and id in ({holes})"  # noqa: S608
        try:
            found = self._db.execute(sql, (self._session, *ids)).fetchall()
        except sqlite3.Error:
            return {}
        return {int(row[0]): row for row in found}

    def info(self) -> tuple[str, str]:
        """``(title, model)`` off the session's own row, empty when it has none."""
        found = self._ask(_INFO_SQL)
        if not found:
            return "", ""
        title, model = found[0]
        return str(title or ""), str(model or "")

    def usage(self) -> list[tuple[Any, ...]]:
        """Every ``session_model_usage`` row for this session."""
        return self._ask(_USAGE_SQL)

    def close(self) -> None:
        """Drop the connection."""
        self._db.close()

    def _ask(self, sql: str) -> list[tuple[Any, ...]]:
        """One parameterised read, or an empty result when the store refuses."""
        try:
            return self._db.execute(sql, (self._session,)).fetchall()
        except sqlite3.Error:
            return []


_STREAM_SQL: Final = "select id from messages where session_id = ? and active = 1 order by id"

_USAGE_SQL: Final = (
    "select model, billing_provider, api_call_count, input_tokens,"
    " output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens,"
    " actual_cost_usd, estimated_cost_usd, cost_status"
    " from session_model_usage where session_id = ?"
)

_INFO_SQL: Final = "select title, model from sessions where id = ? limit 1"

_COST_UNKNOWN: Final = frozenset({"", "none", "unknown"})


def _turn(row: tuple[Any, ...]) -> TranscriptTurn | None:
    """One ``messages`` row as a chat turn, or ``None`` to drop it."""
    identifier, role, content, call_id, calls, tool_name, stamp = row
    uuid = str(identifier)
    ts = _stamp(stamp)
    text = content if isinstance(content, str) else ""
    if role == "user":
        body, cut = _clip(text, MAX_PROSE_CHARS)
        if not body:
            return None
        return TranscriptTurn(uuid=uuid, speaker=Speaker.human, text=body, ts=ts, truncated=cut)
    if role == "tool":
        body, cut = _clip(text, MAX_RESULT_CHARS)
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
    body, cut = _clip(text, MAX_PROSE_CHARS)
    if body:
        return TranscriptTurn(uuid=uuid, speaker=Speaker.agent, text=body, ts=ts, truncated=cut)
    return _call_turn(_calls(calls), uuid=uuid, ts=ts, tool_name=tool_name)


def _call_turn(
    calls: list[dict[str, Any]], *, uuid: str, ts: str, tool_name: object
) -> TranscriptTurn | None:
    """The FIRST tool call of an assistant row, collapsed the way Claude's is."""
    if not calls:
        return None
    first = calls[0]
    function = first.get("function")
    named = function.get("name") if isinstance(function, dict) else None
    params = _arguments(first)
    summary, cut = _tool_summary(params)
    return TranscriptTurn(
        uuid=uuid,
        speaker=Speaker.tool,
        text=summary,
        ts=ts,
        tool=str(named or tool_name or ""),
        truncated=cut,
        call=str(first.get("id") or first.get("call_id") or ""),
        diff=tool_diff(params),
    )


def _charged(row: tuple[Any, ...] | None) -> int:
    """What one row costs the page budget: its text, plus a floor for the rest."""
    if row is None:
        return _ROW_BYTES
    text = sum(len(value) for value in row if isinstance(value, str))
    return text + _ROW_BYTES


def _page(db: _Db, *, limit: int, before: int | None, max_bytes: int) -> TranscriptPage:
    """Up to *limit* turns ending at frontier *before*, oldest first."""
    stream = db.stream()
    size = len(stream)
    end = size if before is None else max(0, min(before, size))
    window = stream[max(0, end - limit) : end]
    rows = db.rows(window)
    turns: list[TranscriptTurn] = []
    cursor = end
    spent = 0
    for position in range(end, max(0, end - limit), -1):
        row = rows.get(stream[position - 1])
        spent += _charged(row)
        if spent > max_bytes:
            break
        cursor = position - 1
        turn = None if row is None else _turn(row)
        if turn is not None:
            turns.append(turn)
            if len(turns) >= limit:
                break
    turns.reverse()
    return TranscriptPage(
        turns=turns,
        cursor=cursor,
        size=size,
        has_more=cursor > 0,
        end=end,
        state=HistoryState.ok if turns else HistoryState.empty,
    )


def _since(db: _Db, *, after: int, limit: int, max_bytes: int) -> TranscriptPage:
    """Whatever the store has grown past frontier *after*, oldest first."""
    stream = db.stream()
    size = len(stream)
    start = max(0, after)
    if start > size:
        return TranscriptPage(size=size, end=size, reset=True, state=HistoryState.empty)
    window = stream[start : start + limit]
    rows = db.rows(window)
    turns: list[TranscriptTurn] = []
    end = start
    spent = 0
    for position in range(start + 1, min(size, start + limit) + 1):
        row = rows.get(stream[position - 1])
        spent += _charged(row)
        if spent > max_bytes:
            break
        end = position
        turn = None if row is None else _turn(row)
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
    )


def _tally(db: _Db) -> SessionUsage:
    """What *session* has spent, off the per-model rollup Hermes already keeps."""
    tally = SessionUsage(state=HistoryState.ok, logs=1)
    for row in db.usage():
        model, provider, calls, prompt, produced, cached, written, thought = row[:8]
        _remember(tally, ModelRef(str(model or ""), str(provider or "")))
        tally.messages += int(calls or 0)
        tally.input_tokens += int(prompt or 0)
        tally.output_tokens += int(produced or 0)
        tally.cache_read_tokens += int(cached or 0)
        tally.cache_creation_tokens += int(written or 0)
        tally.thinking_tokens += int(thought or 0)
        tally.cost += _money(*row[8:11])
    return tally


def _money(actual: object, estimated: object, status: object) -> float:
    """What one model row cost, or 0.0 -- which this app's own field calls UNKNOWN."""
    if str(status or "").strip().lower() in _COST_UNKNOWN:
        return 0.0
    for value in (actual, estimated):
        if isinstance(value, int | float) and value > 0:
            return float(value)
    return 0.0


def _open(roots: Sequence[Path], session: str) -> _Db | None:
    """The first root that holds *session*, or ``None``."""
    for base in roots:
        found = _Db.open(Path(base) / HERMES_DB, session)
        if found is not None:
            return found
    return None


class HermesAdapter:
    """Hermes's history: one SQLite store, paged by POSITION not byte."""

    default_roots: ClassVar[tuple[str, ...]] = (HERMES_ROOT,)

    def page(
        self,
        session: str,
        *,
        roots: Sequence[Path],
        limit: int = 40,
        before: int | None = None,
        max_bytes: int = MAX_PAGE_BYTES,
        expect: str = "",
    ) -> TranscriptPage:
        """Up to *limit* turns ending at *before*, oldest first."""
        # `expect` names which FILE a cursor was taken against. This
        # store is one file for every session and cannot move under a cursor,
        # so it never reports a log and is never asked to check one.
        db = _open(roots, session)
        if db is None:
            return TranscriptPage(state=HistoryState.absent)
        with closing(db):
            return _page(db, limit=limit, before=before, max_bytes=max_bytes)

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
        db = _open(roots, session)
        if db is None:
            return TranscriptPage(state=HistoryState.absent)
        with closing(db):
            return _since(db, after=after, limit=limit, max_bytes=max_bytes)

    def info(self, session: str, *, roots: Sequence[Path] = ()) -> tuple[str, str]:
        """``(title, model)`` off *session*'s own row. Never raises."""
        where = roots or [Path(root) for root in self.default_roots]
        db = _open(where, session)
        if db is None:
            return "", ""
        with closing(db):
            return db.info()

    def tally(
        self, session: str, *, roots: Sequence[Path], max_bytes: int = MAX_USAGE_BYTES
    ) -> SessionUsage:
        """What this session has spent. Hermes records no money, so cost stays 0."""
        db = _open(roots, session)
        if db is None:
            return SessionUsage(state=HistoryState.absent)
        with closing(db):
            return _tally(db)
