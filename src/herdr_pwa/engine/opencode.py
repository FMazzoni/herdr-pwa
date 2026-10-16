"""opencode's store, in either of its two layouts, read as one conversation."""

import re
import sqlite3
from collections.abc import Sequence
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Final, NamedTuple, Protocol, Self, cast, override
from urllib.parse import quote

from herdr_pwa.diff import tool_diff
from herdr_pwa.engine.transcript import (
    MAX_NOTE_CHARS,
    MAX_PAGE_BYTES,
    MAX_PROSE_CHARS,
    MAX_RESULT_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_USAGE_BYTES,
    Gather,
    HistoryState,
    ModelRef,
    SessionUsage,
    Speaker,
    TranscriptAdapter,
    TranscriptPage,
    TranscriptTurn,
    clip,
    one_line,
    remember,
)
from herdr_pwa.jsontypes import JsonObject, as_object, get_object, loads

type _Row = tuple[object, ...]


OPENCODE_ROOT: Final = "~/.local/share/opencode"

OPENCODE_DB: Final = "opencode.db"

OPENCODE_STORAGE_ROOT: Final = f"{OPENCODE_ROOT}/storage"

_BUSY_SECONDS: Final = 0.25


class _LockedError(Exception):
    """The store is being written and would not answer inside the busy wait."""


def _locked(exc: sqlite3.Error) -> bool:
    """Is this failure the writer holding the store, not a broken one?"""
    text = str(exc).lower()
    return "locked" in text or "busy" in text


_ID: Final = re.compile(r"^(?:ses|msg|prt)_[A-Za-z0-9]+$")

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

_QUIET_TYPES: Final = frozenset(
    {"step-start", "step-finish", "snapshot", "patch", "reasoning", "compaction"}
)


def _named(path: Path) -> bool:
    """A store entry this reader will open: an id, a ``.json``, nothing else."""
    return path.suffix == ".json" and bool(_ID.match(path.stem))


def _decode(blob: object) -> JsonObject | None:
    """One stored record as a dict, or ``None`` if it is not one."""
    if not isinstance(blob, str | bytes):
        return None
    try:
        record = loads(blob)
    except (ValueError, UnicodeDecodeError):
        return None
    return as_object(record)


class SessionInfo(NamedTuple):
    """What a session's OWN record says about itself, as opposed to its messages."""

    title: str = ""

    model: ModelRef = ModelRef()


def _text(value: object) -> str:
    """One stored string, trimmed, or ``""`` for anything that is not one."""
    return value.strip() if isinstance(value, str) else ""


def _model_ref(value: object) -> ModelRef:
    """The ``model`` column: ``{id, providerID}`` today, a bare name before it."""
    record = _decode(value)
    if record is None:
        return ModelRef(_text(value))
    return ModelRef(_text(record.get("id")), _text(record.get("providerID")))


class _Store(Protocol):
    """The five questions a page asks of whichever layout holds the session."""

    def stream(self) -> list[tuple[str, str]]:
        """Every part as ``(message id, key)``, oldest first."""
        ...

    def part(self, key: str) -> tuple[JsonObject | None, int]:
        """One part record and what reading it spent, ``None`` if not whole."""
        ...

    def message(self, key: str) -> tuple[JsonObject | None, int]:
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
    """The legacy layout: one JSON file per record under ``storage/``."""

    def __init__(self, base: Path, session: str) -> None:
        self._base = base
        self._session = session

    @classmethod
    def open(cls, base: Path, session: str) -> Self | None:
        """*base* if it holds *session*'s messages, contained to THAT root."""
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
        found: list[tuple[str, str]] = []
        for message in self.messages():
            try:
                parts = sorted((self._base / "part" / message).iterdir())
            except OSError:
                continue
            found.extend((message, str(part)) for part in parts if _named(part))
        return found

    def part(self, key: str) -> tuple[JsonObject | None, int]:
        """One part file, and its size whether or not it parsed."""
        return self._file(Path(key))

    def message(self, key: str) -> tuple[JsonObject | None, int]:
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
        if not _ID.match(self._session):
            return SessionInfo()
        try:
            projects = sorted((self._base / "session").iterdir())
        except OSError:
            return SessionInfo()
        for project in projects:
            record, _ = self._file(project / f"{self._session}.json")
            if record is not None:
                return SessionInfo(title=one_line(record.get("title"))[0])
        return SessionInfo()

    def close(self) -> None:
        """Nothing was held open."""

    def _file(self, path: Path) -> tuple[JsonObject | None, int]:
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
    """*path* as the only URI this module ever hands SQLite."""
    return f"file:{quote(str(path))}?mode=ro"


class _Db:
    """The live layout: one SQLite file, opened read-only and never locked."""

    def __init__(self, connection: sqlite3.Connection, session: str) -> None:
        self._db = connection
        self._session = session

    @classmethod
    def open(cls, path: Path, session: str) -> Self | None:
        """*path* if it is a store we recognise and *session* is in it."""
        try:
            resolved = path.expanduser().resolve(strict=True)
        except OSError:
            return None
        try:
            connection = sqlite3.connect(_readonly(resolved), uri=True, timeout=_BUSY_SECONDS)
            connection.execute("select id from session limit 0").close()
            held = cast(
                _Row | None,
                connection.execute(
                    "select 1 from session where id = ? limit 1", (session,)
                ).fetchone(),
            )
        except ValueError:
            return None
        except OSError:
            return None
        except sqlite3.Error as exc:
            if _locked(exc):
                raise _LockedError from exc
            return None
        if held is None:
            connection.close()
            return None
        return cls(connection, session)

    def stream(self) -> list[tuple[str, str]]:
        """Every part as ``(message id, part id)``, oldest first."""
        rows = self._ask(
            "select message_id, id from part where session_id = ? order by message_id, id"
        )
        return [
            (m, k)
            for m, k in rows
            if isinstance(m, str) and isinstance(k, str) and _ID.match(m) and _ID.match(k)
        ]

    def part(self, key: str) -> tuple[JsonObject | None, int]:
        """One part row, with its own id folded back in as the store spells it."""
        record, spent = self._row("part", key)
        if record is not None:
            record["id"] = key
        return record, spent

    def message(self, key: str) -> tuple[JsonObject | None, int]:
        """One message row."""
        return self._row("message", key)

    def messages(self) -> list[str]:
        """Every message id of the session, oldest first."""
        rows = self._ask("select id from message where session_id = ? order by id")
        return [key for (key,) in rows if isinstance(key, str) and _ID.match(key)]

    def info(self) -> SessionInfo:
        """The session row itself, which is where opencode keeps both of these."""
        found = self._named("title, model")
        if found is None:
            # A store older than the column: narrow the read rather than lose the title.
            found = self._named("title")
        if not found:
            return SessionInfo()
        title, *rest = found
        return SessionInfo(
            title=one_line(title)[0], model=_model_ref(rest[0]) if rest else ModelRef()
        )

    def _named(self, columns: str) -> _Row | None:
        """The session's own row, or ``None`` -- a store without *columns* included."""
        sql = f"select {columns} from session where id = ?"  # noqa: S608
        try:
            with closing(self._db.execute(sql, (self._session,))) as rows:
                return cast(_Row | None, rows.fetchone())
        except (sqlite3.Error, OSError):
            return None

    def close(self) -> None:
        """Give the connection back before the WAL grows around it."""
        with suppress(sqlite3.Error):
            self._db.close()

    def _ask(self, sql: str) -> list[_Row]:
        """One listing query, or an empty listing if the store would not answer."""
        try:
            with closing(self._db.execute(sql, (self._session,))) as rows:
                return cast(list[_Row], rows.fetchall())
        except sqlite3.Error as exc:
            if _locked(exc):
                raise _LockedError from exc
            return []
        except OSError:
            return []

    def _row(self, table: str, key: str) -> tuple[JsonObject | None, int]:
        """One record by id, refusing any name that is not the store's own shape."""
        if not _ID.match(key):
            return None, 0
        sql = f"select data from {table} where id = ? and session_id = ?"  # noqa: S608
        try:
            with closing(self._db.execute(sql, (key, self._session))) as rows:
                found = cast(_Row | None, rows.fetchone())
        except sqlite3.Error as exc:
            if _locked(exc):
                raise _LockedError from exc
            return None, 0
        except OSError:
            return None, 0
        if found is None:
            return None, 0
        blob = found[0]
        spent = len(blob) if isinstance(blob, str | bytes) else 0
        return _decode(blob), spent


def _candidates(root: Path) -> tuple[Path, tuple[Path, Path]]:
    """A configured root as its database and its two legacy spellings."""
    data = root.parent if root.name == "storage" else root
    return data / OPENCODE_DB, (root, data / "storage")


def _open(roots: Sequence[Path], session: str) -> _Store | None:
    """The first root holding *session*, live store before frozen one."""
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


def _moment(part: JsonObject, message: JsonObject) -> str:
    """When a part happened, falling back to when its message was created."""
    # A tool part keeps its clock inside ``state``, where the result also is.
    state = get_object(part, "state")
    times = [
        get_object(part, "time"),
        get_object(state, "time"),
        get_object(message, "time"),
    ]
    for holder in times:
        if holder is None:
            continue
        for key in ("start", "created"):
            stamp = _stamp(holder.get(key))
            if stamp:
                return stamp
    return ""


def _summary(params: object) -> tuple[str, bool]:
    """The one identifying input of a tool call (see `_TOOL_INPUT_KEYS`)."""
    args = as_object(params)
    if args is None:
        return "", False
    for key in _TOOL_INPUT_KEYS:
        if key in args:
            line, cut = one_line(args.get(key), MAX_SUMMARY_CHARS)
            if line:
                return line, cut
    return "", False


def _text_turn(part: JsonObject, uuid: str, ts: str, role: str) -> TranscriptTurn | None:
    """A ``text`` part, where the whole speaker question lives."""
    body = part.get("text")
    if not isinstance(body, str):
        return None
    human = role == "user" and not part.get("synthetic")
    system = role == "user" and bool(part.get("synthetic"))
    text, cut = clip(body, MAX_NOTE_CHARS if system else MAX_PROSE_CHARS)
    if not text:
        return None
    speaker = Speaker.human if human else Speaker.system if system else Speaker.agent
    return TranscriptTurn(uuid=uuid, speaker=speaker, text=text, ts=ts, truncated=cut)


def _tool_turn(part: JsonObject, uuid: str, ts: str) -> TranscriptTurn:
    """A ``tool`` part, which carries the call AND its result in one record."""
    state = get_object(part, "state") or {}
    params = state.get("input")
    text, cut = _summary(params)
    failed = state.get("status") == "error"
    body = state.get("error") if failed else state.get("output")
    result, result_cut = clip(body if isinstance(body, str) else "", MAX_RESULT_CHARS)
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


def _mention_turn(part: JsonObject, uuid: str, ts: str) -> TranscriptTurn | None:
    """A ``file`` or ``agent`` part: the ``@`` a person typed into their prompt."""
    source = get_object(part, "source")
    written = source.get("value") if source is not None else None
    if (inner := get_object(source, "text")) is not None:
        written = inner.get("value") or written
    fallback = part.get("filename") or part.get("name")
    text, cut = one_line(written if isinstance(written, str) else fallback)
    if not text:
        return None
    return TranscriptTurn(uuid=uuid, speaker=Speaker.system, text=text, ts=ts, truncated=cut)


def _turn(part: JsonObject, message: JsonObject) -> TranscriptTurn | None:
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
        self._held: dict[str, JsonObject] = {}
        self.spent = 0

    def get(self, identifier: str) -> JsonObject:
        """*identifier*'s record, from the store or from what was already read."""
        if identifier not in self._held:
            record, spent = self._store.message(identifier)
            self.spent += spent
            self._held[identifier] = record or {}
        return self._held[identifier]


def _charge(
    store: _Store, messages: _Messages, key: str, max_bytes: int
) -> tuple[JsonObject | None, bool]:
    """One part, its read charged to the page; and whether that broke the budget."""
    part, spent = store.part(key)
    messages.spent += spent
    return part, messages.spent > max_bytes


def _page(store: _Store, *, gather: Gather, before: int | None, max_bytes: int) -> TranscriptPage:
    """Turns until *gather* is full, ending at frontier *before*, oldest first."""
    stream = store.stream()
    size = len(stream)
    end = size if before is None else max(0, min(before, size))
    messages = _Messages(store)
    cursor = end
    for position in range(end, 0, -1):
        identifier, key = stream[position - 1]
        part, over = _charge(store, messages, key, max_bytes)
        if over:
            break
        cursor = position - 1
        turn = None if part is None else _turn(part, messages.get(identifier))
        if gather.take(turn, cursor):
            break
    turns = gather.turns
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
    stream = store.stream()
    size = len(stream)
    start = max(0, after)
    if start > size:
        return TranscriptPage(size=size, end=size, reset=True, state=HistoryState.empty)
    messages = _Messages(store)
    gather = Gather(limit)
    turns = gather.turns
    end = start
    for position in range(start + 1, size + 1):
        identifier, key = stream[position - 1]
        part, over = _charge(store, messages, key, max_bytes)
        if over or part is None:
            break
        end = position
        if gather.take(_turn(part, messages.get(identifier)), position - 1):
            break
    return TranscriptPage(
        turns=turns,
        cursor=start,
        size=size,
        end=end,
        has_more=end < size,
        state=HistoryState.ok if turns else HistoryState.empty,
    )


def _count(tokens: JsonObject, key: str) -> int:
    """One token field as a non-negative int, whatever the provider wrote."""
    value = tokens.get(key)
    return value if isinstance(value, int) and value > 0 else 0


def _cached(tokens: JsonObject, key: str) -> int:
    """The read or written half of ``tokens.cache``."""
    cache = get_object(tokens, "cache")
    return _count(cache, key) if cache is not None else 0


def _add(tally: SessionUsage, tokens: JsonObject, ref: ModelRef) -> None:
    """Fold one assistant message's tokens into *tally*, in place."""
    tally.messages += 1
    tally.input_tokens += _count(tokens, "input")
    tally.output_tokens += _count(tokens, "output")
    tally.thinking_tokens += _count(tokens, "reasoning")
    tally.cache_creation_tokens += _cached(tokens, "write")
    tally.cache_read_tokens += _cached(tokens, "read")
    remember(tally, ref)


def _spent(record: JsonObject) -> float:
    """One message's ``cost``, which is money and which only this store records."""
    value = record.get("cost")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value) if value > 0 else 0.0


def _prompt_size(tokens: JsonObject) -> int:
    """Everything that went UP with one message: fresh, cache-written, cache-read."""
    return _count(tokens, "input") + _cached(tokens, "write") + _cached(tokens, "read")


def _clock(tally: SessionUsage, record: JsonObject) -> None:
    """Note when *record* happened, and count it if it is a prompt, which starts a turn."""
    times = get_object(record, "time")
    created = _stamp(times.get("created")) if times is not None else ""
    if created:
        tally.last_at = created
    if record.get("role") == "user":
        tally.prompts += 1
        tally.turn_started = created


def _take(tally: SessionUsage, record: JsonObject) -> None:
    """Fold one message record into *tally*: its time, and an answer's tokens and money."""
    _clock(tally, record)
    if record.get("role") != "assistant":
        return
    tally.cost += _spent(record)
    tokens = get_object(record, "tokens")
    if tokens is None:
        return
    # Every assistant row names both: the provider is not derivable from the model id.
    _add(tally, tokens, ModelRef(_text(record.get("modelID")), _text(record.get("providerID"))))
    tally.context_tokens = _prompt_size(tokens)


def _tally(store: _Store, *, max_bytes: int) -> SessionUsage:
    """What *session* has spent, one message record at a time."""
    tally = SessionUsage()
    budget = max_bytes
    for key in store.messages():
        record, spent = store.message(key)
        budget -= spent
        if budget < 0:
            tally.partial = True
            break
        tally.logs += 1
        if record is not None:
            _take(tally, record)
    if not tally.models:
        # Before its first answer no message names a model, but the session row already does.
        remember(tally, store.info().model)
    tally.state = HistoryState.ok if tally.messages else HistoryState.empty
    return tally


class OpencodeAdapter(TranscriptAdapter):
    """opencode's history: either normalised store, paged by POSITION not byte."""

    default_roots: ClassVar[tuple[str, ...]] = (OPENCODE_ROOT,)

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
        # `expect` is ignored: one directory per session cannot move under a cursor.
        try:
            store = _open(roots, session)
            if store is None:
                return TranscriptPage(state=HistoryState.absent)
            with closing(store):
                gather = Gather(limit, messages)
                return _page(store, gather=gather, before=before, max_bytes=max_bytes)
        except _LockedError:
            # The writer holds the store; the page says so rather than looking empty.
            return TranscriptPage(state=HistoryState.unreadable)

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
        # `expect` is ignored: one directory per session cannot move under a cursor.
        try:
            store = _open(roots, session)
            if store is None:
                return TranscriptPage(state=HistoryState.absent)
            with closing(store):
                return _since(store, after=after, limit=limit, max_bytes=max_bytes)
        except _LockedError:
            return TranscriptPage(state=HistoryState.unreadable)

    @override
    def tally(
        self,
        session: str,
        *,
        roots: Sequence[Path],
        max_bytes: int = MAX_USAGE_BYTES,
    ) -> SessionUsage:
        """What *session* has spent so far, across its message records."""
        try:
            store = _open(roots, session)
            if store is None:
                return SessionUsage(state=HistoryState.absent)
            with closing(store):
                return _tally(store, max_bytes=max_bytes)
        except _LockedError:
            return SessionUsage(state=HistoryState.unreadable)

    def info(self, session: str, *, roots: Sequence[Path] = ()) -> SessionInfo:
        """What *session*'s own record calls it and what it is set to. Never raises."""
        where = roots or [Path(root) for root in self.default_roots]
        try:
            store = _open(where, session)
        except OSError:
            return SessionInfo()
        if store is None:
            return SessionInfo()
        with closing(store):
            return store.info()
