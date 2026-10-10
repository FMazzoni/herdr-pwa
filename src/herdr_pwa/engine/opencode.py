"""opencode's store, in either of its two layouts, read as one conversation.

@doc: docs/modules/engine/opencode.md
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Final, NamedTuple, Protocol
from urllib.parse import quote

from herdr_pwa.diff import tool_diff
from herdr_pwa.engine.transcript import (
    MAX_NOTE_CHARS,
    MAX_PAGE_BYTES,
    MAX_PROSE_CHARS,
    MAX_RESULT_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_USAGE_BYTES,
    HistoryState,
    ModelRef,
    SessionUsage,
    Speaker,
    TranscriptPage,
    TranscriptTurn,
    _clip,
    _one_line,
    _remember,
)

__all__ = [
    "OPENCODE_DB",
    "OPENCODE_ROOT",
    "OPENCODE_STORAGE_ROOT",
    "OpencodeAdapter",
    "SessionInfo",
]

OPENCODE_ROOT: Final = "~/.local/share/opencode"
"""opencode's data directory, which holds BOTH of its stores.

@doc: docs/modules/engine/opencode.md
"""

OPENCODE_DB: Final = "opencode.db"
"""The live store since opencode 1.x: one SQLite file beside ``storage/``.
"""

OPENCODE_STORAGE_ROOT: Final = f"{OPENCODE_ROOT}/storage"
"""The legacy store: ``session/``, ``message/``, ``part/`` as JSON files.
"""

_BUSY_SECONDS: Final = 0.25
"""How long a read waits on a lock before giving up on the whole page.

@doc: docs/modules/engine/opencode/_BUSY_SECONDS.md
"""

_ID: Final = re.compile(r"^(?:ses|msg|prt)_[A-Za-z0-9]+$")
"""The shape of every id in the store, and the only name this reader opens.

@doc: docs/modules/engine/opencode/_ID.md
"""

_TOOL_INPUT_KEYS: Final = (
    "command",
    "filePath",
    "path",
    "pattern",
    "query",
    "url",
    "description",
    "prompt",
    "title",
    "content",
)
"""Which of a tool's inputs IDENTIFIES the call, in preference order.
"""

_QUIET_TYPES: Final = frozenset(
    {"step-start", "step-finish", "snapshot", "patch", "reasoning", "compaction"}
)
"""Part types that carry no conversation.

@doc: docs/modules/engine/opencode/_QUIET_TYPES.md
"""


def _named(path: Path) -> bool:
    """A store entry this reader will open: an id, a ``.json``, nothing else."""
    return path.suffix == ".json" and bool(_ID.match(path.stem))


def _decode(blob: object) -> dict[str, Any] | None:
    """One stored record as a dict, or ``None`` if it is not one."""
    if not isinstance(blob, str | bytes):
        return None
    try:
        record = json.loads(blob)
    except (ValueError, UnicodeDecodeError):
        return None
    return record if isinstance(record, dict) else None


class SessionInfo(NamedTuple):
    """What a session's OWN record says about itself, as opposed to its messages.

    @doc: docs/modules/engine/opencode/SessionInfo.md
    """

    title: str = ""
    """The name opencode gave the session; it generates one after the first turn."""

    model: ModelRef = ModelRef()
    """What the session is POINTED AT, which no message has to have answered yet."""


def _text(value: object) -> str:
    """One stored string, trimmed, or ``""`` for anything that is not one."""
    return value.strip() if isinstance(value, str) else ""


def _model_ref(value: object) -> ModelRef:
    """The ``model`` column: ``{id, providerID}`` today, a bare name before it.

    @doc: docs/modules/engine/opencode/_model_ref.md
    """
    record = _decode(value)
    if record is None:
        return ModelRef(_text(value))
    return ModelRef(_text(record.get("id")), _text(record.get("providerID")))


class _Store(Protocol):
    """The five questions a page asks of whichever layout holds the session.

    @doc: docs/modules/engine/opencode/_Store.md
    """

    def stream(self) -> list[tuple[str, str]]:
        """Every part as ``(message id, key)``, oldest first."""
        ...

    def part(self, key: str) -> tuple[dict[str, Any] | None, int]:
        """One part record and what reading it spent, ``None`` if not whole."""
        ...

    def message(self, key: str) -> tuple[dict[str, Any] | None, int]:
        """One message record and what reading it spent."""
        ...

    def messages(self) -> list[str]:
        """Every message id of the session, oldest first."""
        ...

    def info(self) -> SessionInfo:
        """The session's own record: what it is CALLED and what it is set to."""
        ...

    def close(self) -> None:
        """Release whatever the store held open."""
        ...


class _Files:
    """The legacy layout: one JSON file per record under ``storage/``.

    @doc: docs/modules/engine/opencode.md
    """

    def __init__(self, base: Path, session: str) -> None:
        self._base = base
        self._session = session

    @classmethod
    def open(cls, base: Path, session: str) -> _Files | None:
        """*base* if it holds *session*'s messages, contained to THAT root."""
        # @doc: docs/modules/engine/opencode.md
        try:
            resolved = base.expanduser().resolve(strict=True)
            messages = (resolved / "message" / session).resolve(strict=True)
        except OSError:
            return None
        if not messages.is_relative_to(resolved) or not messages.is_dir():
            return None
        return cls(resolved, session)

    def stream(self) -> list[tuple[str, str]]:
        """Every part as ``(message id, path)``, oldest first."""
        # @doc: docs/modules/engine/opencode.md
        found: list[tuple[str, str]] = []
        for message in self.messages():
            try:
                parts = sorted((self._base / "part" / message).iterdir())
            except OSError:
                continue
            found.extend((message, str(part)) for part in parts if _named(part))
        return found

    def part(self, key: str) -> tuple[dict[str, Any] | None, int]:
        """One part file, and its size whether or not it parsed."""
        return self._file(Path(key))

    def message(self, key: str) -> tuple[dict[str, Any] | None, int]:
        """One message file, and its size whether or not it parsed."""
        return self._file(self._base / "message" / self._session / f"{key}.json")

    def messages(self) -> list[str]:
        """Every message id of the session, oldest first."""
        directory = self._base / "message" / self._session
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return []
        return [entry.stem for entry in entries if _named(entry)]

    def info(self) -> SessionInfo:
        """The session's own JSON, which names it but never says what it is pointed at."""
        # @doc: docs/modules/engine/opencode/SessionInfo.md
        if not _ID.match(self._session):
            return SessionInfo()
        try:
            projects = sorted((self._base / "session").iterdir())
        except OSError:
            return SessionInfo()
        for project in projects:
            record, _ = self._file(project / f"{self._session}.json")
            if record is not None:
                return SessionInfo(title=_one_line(record.get("title"))[0])
        return SessionInfo()

    def close(self) -> None:
        """Nothing was held open."""

    def _file(self, path: Path) -> tuple[dict[str, Any] | None, int]:
        """*path* as a record and a cost, ``None`` while it is not yet whole."""
        try:
            blob = path.read_bytes()
        except OSError:
            try:
                return None, path.stat().st_size
            except OSError:
                return None, 0
        return _decode(blob), len(blob)


def _readonly(path: Path) -> str:
    """*path* as the only URI this module ever hands SQLite.

    @doc: docs/modules/engine/opencode/_readonly.md
    """
    return f"file:{quote(str(path))}?mode=ro"


class _Db:
    """The live layout: one SQLite file, opened read-only and never locked.

    @doc: docs/modules/engine/opencode/_Db.md
    """

    def __init__(self, connection: sqlite3.Connection, session: str) -> None:
        self._db = connection
        self._session = session

    @classmethod
    def open(cls, path: Path, session: str) -> _Db | None:
        """*path* if it is a store we recognise and *session* is in it."""
        # @doc: docs/modules/engine/opencode.md
        try:
            resolved = path.expanduser().resolve(strict=True)
        except OSError:
            return None
        # @doc: docs/modules/engine/opencode.md
        try:
            connection = sqlite3.connect(
                _readonly(resolved), uri=True, timeout=_BUSY_SECONDS
            )
            connection.execute("select id from session limit 0").close()
            held = connection.execute(
                "select 1 from session where id = ? limit 1", (session,)
            ).fetchone()
        except (sqlite3.Error, OSError, ValueError):
            return None
        if held is None:
            connection.close()
            return None
        return cls(connection, session)

    def stream(self) -> list[tuple[str, str]]:
        """Every part as ``(message id, part id)``, oldest first."""
        # @doc: docs/modules/engine/opencode.md
        rows = self._ask(
            "select message_id, id from part where session_id = ?"
            " order by message_id, id"
        )
        return [(m, k) for m, k in rows if _ID.match(m) and _ID.match(k)]

    def part(self, key: str) -> tuple[dict[str, Any] | None, int]:
        """One part row, with its own id folded back in as the store spells it."""
        # @doc: docs/modules/engine/opencode.md
        record, spent = self._row("part", key)
        if record is not None:
            record["id"] = key
        return record, spent

    def message(self, key: str) -> tuple[dict[str, Any] | None, int]:
        """One message row."""
        return self._row("message", key)

    def messages(self) -> list[str]:
        """Every message id of the session, oldest first."""
        rows = self._ask("select id from message where session_id = ? order by id")
        return [key for (key,) in rows if _ID.match(key)]

    def info(self) -> SessionInfo:
        """The session row itself, which is where opencode keeps both of these."""
        # @doc: docs/modules/engine/opencode/SessionInfo.md
        found = self._named("title, model")
        if found is None:
            # A store older than the column: narrow the read rather than lose
            # the title, which is the half this is here for.
            found = self._named("title")
        if not found:
            return SessionInfo()
        title, *rest = found
        return SessionInfo(
            title=_one_line(title)[0], model=_model_ref(rest[0]) if rest else ModelRef()
        )

    def _named(self, columns: str) -> tuple[Any, ...] | None:
        """The session's own row, or ``None`` -- a store without *columns* included."""
        sql = f"select {columns} from session where id = ?"  # noqa: S608
        try:
            with closing(self._db.execute(sql, (self._session,))) as rows:
                return rows.fetchone()
        except (sqlite3.Error, OSError):
            return None

    def close(self) -> None:
        """Give the connection back before the WAL grows around it."""
        with suppress(sqlite3.Error):
            self._db.close()

    def _ask(self, sql: str) -> list[tuple[Any, ...]]:
        """One listing query, or an empty listing if the store would not answer."""
        try:
            with closing(self._db.execute(sql, (self._session,))) as rows:
                return rows.fetchall()
        except (sqlite3.Error, OSError):
            return []

    def _row(self, table: str, key: str) -> tuple[dict[str, Any] | None, int]:
        """One record by id, refusing any name that is not the store's own shape."""
        if not _ID.match(key):
            return None, 0
        sql = f"select data from {table} where id = ? and session_id = ?"  # noqa: S608
        try:
            with closing(self._db.execute(sql, (key, self._session))) as rows:
                found = rows.fetchone()
        except (sqlite3.Error, OSError):
            return None, 0
        if found is None:
            return None, 0
        blob = found[0]
        spent = len(blob) if isinstance(blob, str | bytes) else 0
        return _decode(blob), spent


def _candidates(root: Path) -> tuple[Path, tuple[Path, Path]]:
    """A configured root as its database and its two legacy spellings."""
    # @doc: docs/modules/engine/opencode.md
    data = root.parent if root.name == "storage" else root
    return data / OPENCODE_DB, (root, data / "storage")


def _open(roots: Sequence[Path], session: str) -> _Store | None:
    """The first root holding *session*, live store before frozen one."""
    # @doc: docs/modules/engine/opencode.md
    for root in roots:
        database, legacy = _candidates(root.expanduser())
        live = _Db.open(database, session)
        if live is not None:
            return live
        for base in legacy:
            frozen = _Files.open(base, session)
            if frozen is not None:
                return frozen
    return None


def _stamp(value: object) -> str:
    """A millisecond epoch as the ISO 8601 the chat's day and clock filters want."""
    if not isinstance(value, int | float) or value <= 0:
        return ""
    return datetime.fromtimestamp(value / 1000, UTC).isoformat().replace("+00:00", "Z")


def _moment(part: dict[str, Any], message: dict[str, Any]) -> str:
    """When a part happened, falling back to when its message was created."""
    # A tool part keeps its clock inside ``state``, where the result also is.
    state = part.get("state")
    times = [
        part.get("time"),
        state.get("time") if isinstance(state, dict) else None,
        message.get("time"),
    ]
    for holder in times:
        if not isinstance(holder, dict):
            continue
        for key in ("start", "created"):
            stamp = _stamp(holder.get(key))
            if stamp:
                return stamp
    return ""


def _summary(params: object) -> tuple[str, bool]:
    """The one identifying input of a tool call (see `_TOOL_INPUT_KEYS`)."""
    if not isinstance(params, dict):
        return "", False
    for key in _TOOL_INPUT_KEYS:
        if key in params:
            line, cut = _one_line(params.get(key), MAX_SUMMARY_CHARS)
            if line:
                return line, cut
    return "", False


def _text_turn(
    part: dict[str, Any], uuid: str, ts: str, role: str
) -> TranscriptTurn | None:
    """A ``text`` part, where the whole speaker question lives."""
    # @doc: docs/modules/engine/opencode.md
    body = part.get("text")
    if not isinstance(body, str):
        return None
    human = role == "user" and not part.get("synthetic")
    system = role == "user" and bool(part.get("synthetic"))
    text, cut = _clip(body, MAX_NOTE_CHARS if system else MAX_PROSE_CHARS)
    if not text:
        return None
    speaker = Speaker.human if human else Speaker.system if system else Speaker.agent
    return TranscriptTurn(uuid=uuid, speaker=speaker, text=text, ts=ts, truncated=cut)


def _tool_turn(part: dict[str, Any], uuid: str, ts: str) -> TranscriptTurn:
    """A ``tool`` part, which carries the call AND its result in one record."""
    # @doc: docs/modules/engine/opencode.md
    state = part.get("state")
    state = state if isinstance(state, dict) else {}
    params = state.get("input")
    text, cut = _summary(params)
    failed = state.get("status") == "error"
    body = state.get("error") if failed else state.get("output")
    result, result_cut = _clip(body if isinstance(body, str) else "", MAX_RESULT_CHARS)
    return TranscriptTurn(
        uuid=uuid,
        speaker=Speaker.tool,
        text=text,
        ts=ts,
        tool=str(part.get("tool") or ""),
        truncated=cut,
        call=str(part.get("callID") or ""),
        result=result,
        failed=failed,
        result_truncated=result_cut,
        diff=tool_diff(params),
    )


def _mention_turn(part: dict[str, Any], uuid: str, ts: str) -> TranscriptTurn | None:
    """A ``file`` or ``agent`` part: the ``@`` a person typed into their prompt."""
    source = part.get("source")
    written = source.get("value") if isinstance(source, dict) else None
    if isinstance(source, dict) and isinstance(source.get("text"), dict):
        written = source["text"].get("value") or written
    fallback = part.get("filename") or part.get("name")
    text, cut = _one_line(written if isinstance(written, str) else fallback)
    if not text:
        return None
    return TranscriptTurn(
        uuid=uuid, speaker=Speaker.system, text=text, ts=ts, truncated=cut
    )


def _turn(part: dict[str, Any], message: dict[str, Any]) -> TranscriptTurn | None:
    """One part as a chat turn, or ``None`` for the machinery around it."""
    kind = part.get("type")
    uuid = part.get("id")
    if kind in _QUIET_TYPES or not isinstance(uuid, str) or not uuid:
        return None
    role = message.get("role")
    ts = _moment(part, message)
    if kind == "text":
        return _text_turn(part, uuid, ts, role if isinstance(role, str) else "")
    if kind == "tool":
        return _tool_turn(part, uuid, ts)
    if kind in ("file", "agent"):
        return _mention_turn(part, uuid, ts)
    return None


class _Messages:
    """The message records a page needs, read once each."""

    def __init__(self, store: _Store) -> None:
        self._store = store
        self._held: dict[str, dict[str, Any]] = {}
        self.spent = 0

    def get(self, identifier: str) -> dict[str, Any]:
        """*identifier*'s record, from the store or from what was already read."""
        if identifier not in self._held:
            record, spent = self._store.message(identifier)
            self.spent += spent
            self._held[identifier] = record or {}
        return self._held[identifier]


def _page(
    store: _Store, *, limit: int, before: int | None, max_bytes: int
) -> TranscriptPage:
    """Up to *limit* turns ending at frontier *before*, oldest first."""
    # @doc: docs/modules/engine/opencode.md
    stream = store.stream()
    size = len(stream)
    end = size if before is None else max(0, min(before, size))
    messages = _Messages(store)
    turns: list[TranscriptTurn] = []
    cursor = end
    for position in range(end, 0, -1):
        identifier, key = stream[position - 1]
        part, spent = store.part(key)
        messages.spent += spent
        if messages.spent > max_bytes:
            break
        cursor = position - 1
        turn = None if part is None else _turn(part, messages.get(identifier))
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


def _since(store: _Store, *, after: int, limit: int, max_bytes: int) -> TranscriptPage:
    """Whatever the store has grown past frontier *after*, oldest first."""
    # @doc: docs/modules/engine/opencode.md
    stream = store.stream()
    size = len(stream)
    start = max(0, after)
    if start > size:
        return TranscriptPage(size=size, end=size, reset=True, state=HistoryState.empty)
    messages = _Messages(store)
    turns: list[TranscriptTurn] = []
    end = start
    for position in range(start + 1, size + 1):
        identifier, key = stream[position - 1]
        part, spent = store.part(key)
        messages.spent += spent
        if messages.spent > max_bytes:
            break
        if part is None:
            break
        end = position
        turn = _turn(part, messages.get(identifier))
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


def _count(tokens: dict[str, Any], key: str) -> int:
    """One token field as a non-negative int, whatever the provider wrote."""
    value = tokens.get(key)
    return value if isinstance(value, int) and value > 0 else 0


def _cached(tokens: dict[str, Any], key: str) -> int:
    """The read or written half of ``tokens.cache``."""
    cache = tokens.get("cache")
    return _count(cache, key) if isinstance(cache, dict) else 0


def _add(tally: SessionUsage, tokens: dict[str, Any], ref: ModelRef) -> None:
    """Fold one assistant message's tokens into *tally*, in place."""
    tally.messages += 1
    tally.input_tokens += _count(tokens, "input")
    tally.output_tokens += _count(tokens, "output")
    tally.thinking_tokens += _count(tokens, "reasoning")
    tally.cache_creation_tokens += _cached(tokens, "write")
    tally.cache_read_tokens += _cached(tokens, "read")
    _remember(tally, ref)


def _spent(record: dict[str, Any]) -> float:
    """One message's ``cost``, which is money and which only this store records."""
    value = record.get("cost")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value) if value > 0 else 0.0


def _prompt_size(tokens: dict[str, Any]) -> int:
    """Everything that went UP with one message: fresh, cache-written, cache-read."""
    return _count(tokens, "input") + _cached(tokens, "write") + _cached(tokens, "read")


def _tally(store: _Store, *, max_bytes: int) -> SessionUsage:
    """What *session* has spent, one message record at a time."""
    # @doc: docs/modules/engine/opencode.md
    tally = SessionUsage()
    budget = max_bytes
    for key in store.messages():
        record, spent = store.message(key)
        budget -= spent
        if budget < 0:
            tally.partial = True
            break
        tally.logs += 1
        if record is None or record.get("role") != "assistant":
            continue
        tally.cost += _spent(record)
        tokens = record.get("tokens")
        if not isinstance(tokens, dict):
            continue
        # Every assistant row names BOTH, because which provider served a
        # model is not derivable from the model id.
        _add(
            tally,
            tokens,
            ModelRef(_text(record.get("modelID")), _text(record.get("providerID"))),
        )
        tally.context_tokens = _prompt_size(tokens)
    if not tally.models:
        # No message has named a model, which is every session before its first
        # answer -- and the session row names one from the moment it is created.
        _remember(tally, store.info().model)
    tally.state = HistoryState.ok if tally.messages else HistoryState.empty
    return tally


class OpencodeAdapter:
    """opencode's history: either normalised store, paged by POSITION not byte.

    @doc: docs/modules/engine/opencode.md
    """

    default_roots: ClassVar[tuple[str, ...]] = (OPENCODE_ROOT,)

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
        # `expect` is ignored here for the same reason `since` ignores it: this
        # store is one directory per session and cannot move under a cursor.
        store = _open(roots, session)
        if store is None:
            return TranscriptPage(state=HistoryState.absent)
        with closing(store):
            return _page(store, limit=limit, before=before, max_bytes=max_bytes)

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
        # `expect` names which FILE a cursor was taken against; this
        # store is one directory per session and cannot move under a cursor,
        # so it never reports a log and is never asked to check one.
        store = _open(roots, session)
        if store is None:
            return TranscriptPage(state=HistoryState.absent)
        with closing(store):
            return _since(store, after=after, limit=limit, max_bytes=max_bytes)

    def tally(
        self,
        session: str,
        *,
        roots: Sequence[Path],
        max_bytes: int = MAX_USAGE_BYTES,
    ) -> SessionUsage:
        """What *session* has spent so far, across its message records."""
        store = _open(roots, session)
        if store is None:
            return SessionUsage(state=HistoryState.absent)
        with closing(store):
            return _tally(store, max_bytes=max_bytes)

    def info(self, session: str, *, roots: Sequence[Path] = ()) -> SessionInfo:
        """What *session*'s own record calls it and what it is set to. Never raises."""
        # @doc: docs/modules/engine/opencode/SessionInfo.md
        where = roots or [Path(root) for root in self.default_roots]
        try:
            store = _open(where, session)
        except OSError:
            return SessionInfo()
        if store is None:
            return SessionInfo()
        with closing(store):
            return store.info()
