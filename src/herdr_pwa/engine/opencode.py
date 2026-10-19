"""opencode's store, in either of its two layouts, read as one conversation."""

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, ClassVar, Final, NamedTuple, Protocol, Self, override

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

from herdr_pwa.engine.notices import INTERRUPTED_CODE, ApiError, api_error, status_code
from herdr_pwa.engine.store import SessionStore, consult
from herdr_pwa.engine.transcript import (
    MAX_NOTE_CHARS,
    MAX_PROSE_CHARS,
    MAX_RESULT_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_USAGE_BYTES,
    Gather,
    Harness,
    HistoryState,
    ModelRef,
    SessionUsage,
    Source,
    Speaker,
    Spend,
    StartRecipe,
    TranscriptAdapter,
    TranscriptPage,
    TranscriptTurn,
    Window,
    api_error_turn,
    clip,
    first_line,
    one_line,
    remember,
    spend_of,
    token_count,
)
from herdr_pwa.validation import OR_NONE, Count, Flag, Number, Text

# opencode auto-approves bash by default, so its agent would never block and never notify.
OPENCODE_ASK: Final = json.dumps({"permission": {"bash": "ask"}}, separators=(",", ":"))

OPENCODE_ROOT: Final = "~/.local/share/opencode"

OPENCODE_DB: Final = "opencode.db"

OPENCODE_STORAGE_ROOT: Final = f"{OPENCODE_ROOT}/storage"

_ID: Final = re.compile(r"^(?:ses|msg|prt)_[A-Za-z0-9]+$")

_QUIET_TYPES: Final = frozenset(
    {"step-start", "step-finish", "snapshot", "patch", "reasoning", "compaction"}
)

# Measured: the error opencode records when the turn was stopped, worded "Aborted" or, in the
# frozen layout, "The operation was aborted."
_ABORTED: Final = "MessageAbortedError"

# A message whose answer failed: its error is read where its parts end, as one more entry.
_FAILED_MESSAGES: Final = (
    "select id from message where session_id = ?"
    " and case when json_valid(data) then json_type(data, '$.error') end = 'object'"
)


def _named(path: Path) -> bool:
    """A store entry this reader will open: an id, a ``.json``, nothing else."""
    return path.suffix == ".json" and bool(_ID.match(path.stem))


# What opencode's records hold, as far as this reader looks: each model ignores the rest.


class _ToolInput(BaseModel):
    """A tool part's input, as far as naming the call goes."""

    model_config = ConfigDict(extra="ignore")

    file_path: Text = Field(default=None, validation_alias=AliasChoices("file_path", "filePath"))
    path: Text = None
    content: Text = None
    command: Text = None
    pattern: Text = None
    query: Text = None
    url: Text = None
    description: Text = None
    prompt: Text = None
    title: Text = None

    def naming(self) -> tuple[str | None, ...]:
        """The arguments that can name the call, most telling first."""
        return (
            self.command,
            self.file_path,
            self.path,
            self.pattern,
            self.query,
            self.url,
            self.description,
            self.prompt,
            self.title,
            self.content,
        )


class _Times(BaseModel):
    model_config = ConfigDict(extra="ignore")

    created: Number = None


type _When = Annotated[_Times | None, OR_NONE]


class _State(BaseModel):
    """A tool part's ``state``: the call's input and, once it ran, its result."""

    model_config = ConfigDict(extra="ignore")

    input: Annotated[_ToolInput | None, OR_NONE] = None
    status: Text = None
    error: Text = None
    output: Text = None


class _Said(BaseModel):
    model_config = ConfigDict(extra="ignore")

    value: Text = None


class _MentionSource(BaseModel):
    """Where a mention came from: what the person typed for it."""

    model_config = ConfigDict(extra="ignore")

    value: Text = None
    text: Annotated[_Said | None, OR_NONE] = None


class _Part(BaseModel):
    """One ``part`` record: a run of text, a tool call with its result, or a mention."""

    model_config = ConfigDict(extra="ignore")

    id: Text = None
    type: Text = None
    text: Text = None
    synthetic: Flag = None
    tool: Text = None
    call: Text = Field(default=None, validation_alias="callID")
    state: Annotated[_State | None, OR_NONE] = None
    source: Annotated[_MentionSource | None, OR_NONE] = None
    filename: Text = None
    name: Text = None


class _Cache(BaseModel):
    model_config = ConfigDict(extra="ignore")

    read: Count = None
    write: Count = None


class _Tokens(BaseModel):
    """What one answer cost, in tokens."""

    model_config = ConfigDict(extra="ignore")

    input: Count = None
    output: Count = None
    reasoning: Count = None
    cache: Annotated[_Cache | None, OR_NONE] = None


class _ErrorData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: Text = None
    status: Count = Field(default=None, validation_alias="statusCode")


class _Error(BaseModel):
    """A message's ``error``: opencode's name for what went wrong, and the provider's words."""

    model_config = ConfigDict(extra="ignore")

    name: Text = None
    data: Annotated[_ErrorData | None, OR_NONE] = None


class _MessageRecord(BaseModel):
    """One ``message`` record: who spoke, when, on an answer what it cost, and why it failed."""

    model_config = ConfigDict(extra="ignore")

    role: Text = None
    time: _When = None
    cost: Number = None
    tokens: Annotated[_Tokens | None, OR_NONE] = None
    model: Text = Field(default=None, validation_alias="modelID")
    provider: Text = Field(default=None, validation_alias="providerID")
    error: Annotated[_Error | None, OR_NONE] = None


class _Session(BaseModel):
    """A legacy session's own JSON file."""

    model_config = ConfigDict(extra="ignore")

    title: Text = None
    directory: Text = None


class _ModelColumn(BaseModel):
    """The session row's ``model``, as opencode writes it today."""

    model_config = ConfigDict(extra="ignore")

    id: Text = None
    provider: Text = Field(default=None, validation_alias="providerID")


def _decode[M: BaseModel](model: type[M], blob: object) -> M | None:
    """One stored record as *model*, or ``None`` if it is not one."""
    if not isinstance(blob, str | bytes):
        return None
    try:
        return model.model_validate_json(blob)
    except ValidationError:
        return None


class SessionInfo(NamedTuple):
    """What a session's OWN record says about itself, as opposed to its messages."""

    title: str = ""

    model: ModelRef = ModelRef()


def _text(value: object) -> str:
    """One stored string, trimmed, or ``""`` for anything that is not one."""
    return value.strip() if isinstance(value, str) else ""


def _model_ref(value: object) -> ModelRef:
    """The ``model`` column: ``{id, providerID}`` today, a bare name before it."""
    record = _decode(_ModelColumn, value)
    if record is None:
        return ModelRef(_text(value))
    return ModelRef(_text(record.id), _text(record.provider))


class _Store(Protocol):
    """The five questions a page asks of whichever layout holds the session."""

    def stream(self) -> list[tuple[str, str]]:
        """Every part as ``(message id, key)``, oldest first."""
        ...

    def part(self, key: str) -> tuple[_Part | None, int]:
        """One part record and what reading it spent, ``None`` if not whole."""
        ...

    def message(self, key: str) -> tuple[_MessageRecord | None, int]:
        """One message record and what reading it spent."""
        ...

    def messages(self) -> list[str]:
        """Every message id of the session, oldest first."""
        ...

    def info(self) -> SessionInfo:
        """The session's own record: what it is CALLED and what it is set to."""
        ...

    def origin(self) -> str:
        """The directory the session's own record says it was started in."""
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
        """Every part as ``(message id, path)``, then a failed message's own id, oldest first."""
        found: list[tuple[str, str]] = []
        for message in self.messages():
            try:
                parts = sorted((self._base / "part" / message).iterdir())
            except OSError:
                parts = []
            found.extend((message, str(part)) for part in parts if _named(part))
            record, _ = self.message(message)
            if record is not None and record.error is not None:
                found.append((message, message))
        return found

    def part(self, key: str) -> tuple[_Part | None, int]:
        """One part file, and its size whether or not it parsed."""
        blob, spent = self._file(Path(key))
        return _decode(_Part, blob), spent

    def message(self, key: str) -> tuple[_MessageRecord | None, int]:
        """One message file, and its size whether or not it parsed."""
        blob, spent = self._file(self._base / "message" / self._session / f"{key}.json")
        return _decode(_MessageRecord, blob), spent

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
        record = self._own(projects)
        return SessionInfo() if record is None else SessionInfo(title=one_line(record.title)[0])

    def origin(self) -> str:
        """The ``directory`` of the session's own JSON."""
        if not _ID.match(self._session):
            return ""
        try:
            projects = sorted((self._base / "session").iterdir())
        except OSError:
            return ""
        record = self._own(projects)
        return "" if record is None else _text(record.directory)

    def _own(self, projects: list[Path]) -> _Session | None:
        """The session's own JSON, under whichever project directory holds it."""
        for project in projects:
            blob, _ = self._file(project / f"{self._session}.json")
            record = _decode(_Session, blob)
            if record is not None:
                return record
        return None

    def close(self) -> None:
        """Nothing was held open."""

    def _file(self, path: Path) -> tuple[bytes | None, int]:
        """*path*'s bytes and their cost, ``None`` while it cannot be read."""
        try:
            blob = path.read_bytes()
        except OSError:
            try:
                return None, path.stat().st_size
            except OSError:
                return None, 0
        return blob, len(blob)


class _Live:
    """The live layout: one SQLite file, read through the shared store reader."""

    def __init__(self, db: SessionStore) -> None:
        self._db = db

    @classmethod
    def open(cls, path: Path, session: str) -> Self | None:
        """*path* if it is a store we recognise and *session* is in it."""
        db = SessionStore.open(path, table="session", session=session)
        return None if db is None else cls(db)

    def stream(self) -> list[tuple[str, str]]:
        """Every part as ``(message id, part id)``, then a failed message's own id, oldest first."""
        rows = self._db.rows(
            "select message_id, id from part where session_id = ? order by message_id, id",
            self._db.session,
        )
        failed = self._db.rows(_FAILED_MESSAGES, self._db.session)
        found = [
            (m, k)
            for m, k in [*rows, *((m, m) for (m,) in failed)]
            if isinstance(m, str) and isinstance(k, str) and _ID.match(m) and _ID.match(k)
        ]
        return sorted(found, key=_after_parts)

    def part(self, key: str) -> tuple[_Part | None, int]:
        """One part row, with its own id folded back in as the store spells it."""
        blob, spent = self._row("part", key)
        record = _decode(_Part, blob)
        if record is not None:
            record.id = key
        return record, spent

    def message(self, key: str) -> tuple[_MessageRecord | None, int]:
        """One message row."""
        blob, spent = self._row("message", key)
        return _decode(_MessageRecord, blob), spent

    def messages(self) -> list[str]:
        """Every message id of the session, oldest first."""
        rows = self._db.rows(
            "select id from message where session_id = ? order by id", self._db.session
        )
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
            title=one_line(title if isinstance(title, str) else None)[0],
            model=_model_ref(rest[0]) if rest else ModelRef(),
        )

    def origin(self) -> str:
        """The session row's ``directory`` column."""
        found = self._named("directory")
        return _text(found[0]) if found else ""

    def _named(self, columns: str) -> tuple[object, ...] | None:
        """The session's own row, or ``None`` -- a store without *columns* included."""
        sql = f"select {columns} from session where id = ?"  # noqa: S608
        return self._db.first(sql, self._db.session)

    def close(self) -> None:
        """Give the connection back before the WAL grows around it."""
        self._db.close()

    def _row(self, table: str, key: str) -> tuple[object, int]:
        """One record's stored blob by id, refusing any name that is not the store's shape."""
        if not _ID.match(key):
            return None, 0
        sql = f"select data from {table} where id = ? and session_id = ?"  # noqa: S608
        found = self._db.first(sql, key, self._db.session)
        if found is None:
            return None, 0
        blob = found[0]
        return blob, len(blob) if isinstance(blob, str | bytes) else 0


def _after_parts(entry: tuple[str, str]) -> tuple[str, bool, str]:
    """A stream entry's place: by message, a failure (keyed by its own id) after the parts."""
    message, key = entry
    return message, key == message, key


def _candidates(root: Path) -> tuple[Path, tuple[Path, Path]]:
    """A configured root as its database and its two legacy spellings."""
    data = root.parent if root.name == "storage" else root
    return data / OPENCODE_DB, (root, data / "storage")


def _open(roots: Sequence[Path], session: str) -> _Store | None:
    """The first root holding *session*, live store before frozen one."""
    for root in roots:
        database, legacy = _candidates(root.expanduser())
        live = _Live.open(database, session)
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


def _text_turn(part: _Part, uuid: str, role: str) -> TranscriptTurn | None:
    """A ``text`` part, where the whole speaker question lives."""
    body = part.text
    if body is None:
        return None
    human = role == "user" and not part.synthetic
    system = role == "user" and part.synthetic is True
    text, cut = clip(body, MAX_NOTE_CHARS if system else MAX_PROSE_CHARS)
    if not text:
        return None
    speaker = Speaker.human if human else Speaker.system if system else Speaker.agent
    return TranscriptTurn(uuid=uuid, speaker=speaker, text=text, truncated=cut)


def _tool_turn(part: _Part, uuid: str) -> TranscriptTurn:
    """A ``tool`` part, which carries the call AND its result in one record."""
    state = part.state or _State()
    args = state.input
    text, cut = ("", False) if args is None else first_line(args.naming(), MAX_SUMMARY_CHARS)
    failed = state.status == "error"
    body = state.error if failed else state.output
    result, result_cut = clip(body or "", MAX_RESULT_CHARS)
    return TranscriptTurn(
        uuid=uuid,
        speaker=Speaker.tool,
        text=text,
        tool=part.tool or "",
        truncated=cut,
        call=part.call or "",
        result=result,
        failed=failed,
        result_truncated=result_cut,
    )


def _mention_turn(part: _Part, uuid: str) -> TranscriptTurn | None:
    """A ``file`` or ``agent`` part: the ``@`` a person typed into their prompt."""
    source = part.source or _MentionSource()
    written = source.value
    if source.text is not None:
        written = source.text.value or written
    text, cut = one_line(written if written is not None else (part.filename or part.name))
    if not text:
        return None
    return TranscriptTurn(uuid=uuid, speaker=Speaker.system, text=text, truncated=cut)


def _turn(part: _Part, message: _MessageRecord) -> TranscriptTurn | None:
    """One part as a chat turn, or ``None`` for the machinery around it."""
    kind = part.type
    uuid = part.id
    if kind in _QUIET_TYPES or not uuid:
        return None
    if kind == "text":
        return _text_turn(part, uuid, message.role or "")
    if kind == "tool":
        return _tool_turn(part, uuid)
    if kind in ("file", "agent"):
        return _mention_turn(part, uuid)
    return None


class _Messages:
    """The message records a page needs, read once each."""

    def __init__(self, store: _Store) -> None:
        self._store = store
        self._held: dict[str, _MessageRecord] = {}
        self.spent = 0

    def get(self, identifier: str) -> _MessageRecord:
        """*identifier*'s record, from the store or from what was already read."""
        if identifier not in self._held:
            record, spent = self._store.message(identifier)
            self.spent += spent
            self._held[identifier] = record or _MessageRecord()
        return self._held[identifier]


class _Read(NamedTuple):
    """One stream entry as a turn: whether its record was intact, and whether it broke the budget."""

    turn: TranscriptTurn | None
    intact: bool
    over: bool


def _failure(message: _MessageRecord) -> ApiError | None:
    """Why *message*'s answer never came, in the words the chat says it with, or ``None``."""
    error = message.error
    if error is None:
        return None
    data = error.data or _ErrorData()
    code = INTERRUPTED_CODE if error.name == _ABORTED else status_code(data.status)
    return api_error(code, data.message or error.name or "")


def _read(store: _Store, messages: _Messages, entry: tuple[str, str], max_bytes: int) -> _Read:
    """One stream entry, its read charged to the page: a part, or a failed message's error."""
    identifier, key = entry
    if key == identifier:
        message = messages.get(identifier)
        found = _failure(message)
        turn = None if found is None else api_error_turn(found, key)
        return _Read(turn, intact=True, over=messages.spent > max_bytes)
    part, spent = store.part(key)
    messages.spent += spent
    if messages.spent > max_bytes:
        return _Read(None, intact=part is not None, over=True)
    turn = None if part is None else _turn(part, messages.get(identifier))
    return _Read(turn, intact=part is not None, over=False)


def _page(store: _Store, window: Window) -> TranscriptPage:
    """The *window*'s turns or messages, ending at frontier ``before``, oldest first."""
    stream = store.stream()
    size = len(stream)
    before = window.before
    end = size if before is None else max(0, min(before, size))
    gather = Gather(window)
    messages = _Messages(store)
    cursor = end
    for position in range(end, 0, -1):
        read = _read(store, messages, stream[position - 1], gather.budget)
        if read.over:
            break
        cursor = position - 1
        if gather.take(read.turn, cursor):
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


def _since(store: _Store, window: Window) -> TranscriptPage:
    """Whatever the store has grown past the *window*'s frontier ``after``, oldest first."""
    stream = store.stream()
    size = len(stream)
    start = max(0, window.after or 0)
    if start > size:
        return TranscriptPage(size=size, end=size, reset=True, state=HistoryState.empty)
    messages = _Messages(store)
    gather = Gather(window)
    turns = gather.turns
    end = start
    for position in range(start + 1, size + 1):
        read = _read(store, messages, stream[position - 1], gather.budget)
        if read.over or not read.intact:
            break
        end = position
        if gather.take(read.turn, position - 1):
            break
    return TranscriptPage(
        turns=turns,
        cursor=start,
        size=size,
        end=end,
        has_more=end < size,
        state=HistoryState.ok if turns else HistoryState.empty,
    )


def _cache(tokens: _Tokens) -> _Cache:
    """``tokens.cache``, empty when the provider wrote none."""
    return tokens.cache or _Cache()


def _add(spend: Spend, tokens: _Tokens, cost: float) -> None:
    """Fold one assistant message's tokens and money into *spend*, in place."""
    spend.messages += 1
    spend.input_tokens += token_count(tokens.input)
    spend.output_tokens += token_count(tokens.output)
    spend.thinking_tokens += token_count(tokens.reasoning)
    spend.cache_creation_tokens += token_count(_cache(tokens).write)
    spend.cache_read_tokens += token_count(_cache(tokens).read)
    spend.cost += cost


def _spent(record: _MessageRecord) -> float:
    """One message's ``cost``, which is money and which only this store records."""
    value = record.cost
    return float(value) if value is not None and value > 0 else 0.0


def _prompt_size(tokens: _Tokens) -> int:
    """Everything that went UP with one message: fresh, cache-written, cache-read."""
    cache = _cache(tokens)
    return token_count(tokens.input) + token_count(cache.write) + token_count(cache.read)


def _clock(tally: SessionUsage, record: _MessageRecord) -> None:
    """Note when *record* happened, and count it if it is a prompt, which starts a turn."""
    created = _stamp(record.time.created) if record.time is not None else ""
    if created:
        tally.last_at = created
    if record.role == "user":
        tally.prompts += 1
        tally.turn_started = created


def _take(tally: SessionUsage, record: _MessageRecord) -> None:
    """Fold one message record into *tally*: its time, and an answer's tokens and money."""
    _clock(tally, record)
    if record.role != "assistant":
        return
    cost = _spent(record)
    tokens = record.tokens
    # A failed answer's zeros are not a reply: counted, they name a model that never answered
    # and empty the context window the last real answer measured.
    if tokens is None or record.error is not None:
        tally.cost += cost
        return
    # Every assistant row names both: the provider is not derivable from the model id.
    ref = ModelRef(_text(record.model), _text(record.provider))
    _add(tally, tokens, cost)
    remember(tally, ref)
    row = spend_of(tally, ref)
    if row is not None:
        _add(row, tokens, cost)
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
    # OPENCODE_CONFIG_CONTENT is merged over the project's config: https://opencode.ai/docs/config/
    # ``--prompt`` is in ``opencode --help`` (1.18.30), and it opens the session in half the time.
    harness: ClassVar[Harness] = Harness(
        "opencode",
        StartRecipe(
            "opencode",
            models_setting="opencode_models",
            env={"OPENCODE_CONFIG_CONTENT": OPENCODE_ASK},
            prompt_flag="--prompt",
        ),
    )

    @override
    def page(self, source: Source, window: Window) -> TranscriptPage:
        """The *window*'s turns or messages, ending at frontier ``before``, oldest first."""
        # `expect` is ignored: one directory per session cannot move under a cursor.
        return consult(
            lambda: _open(source.roots, source.session),
            lambda store: _page(store, window),
            absent=TranscriptPage(state=HistoryState.absent),
            locked=TranscriptPage(state=HistoryState.unreadable),
        )

    @override
    def since(self, source: Source, window: Window) -> TranscriptPage:
        """Whatever the store has grown past the *window*'s frontier ``after``, oldest first."""
        # `expect` is ignored: one directory per session cannot move under a cursor.
        return consult(
            lambda: _open(source.roots, source.session),
            lambda store: _since(store, window),
            absent=TranscriptPage(state=HistoryState.absent),
            locked=TranscriptPage(state=HistoryState.unreadable),
        )

    @override
    def tally(self, source: Source, *, max_bytes: int = MAX_USAGE_BYTES) -> SessionUsage:
        """What the session has spent so far, across its message records."""
        return consult(
            lambda: _open(source.roots, source.session),
            lambda store: _tally(store, max_bytes=max_bytes),
            absent=SessionUsage(state=HistoryState.absent),
            locked=SessionUsage(state=HistoryState.unreadable),
        )

    @override
    def origin(self, source: Source) -> str:
        """The directory opencode recorded the session as started in, or ``""``."""
        return consult(lambda: _open(source.roots, source.session), _origin, absent="", locked="")

    def info(self, source: Source) -> SessionInfo:
        """What the session's own record calls it and what it is set to. Never raises."""
        where = source.roots or [Path(root) for root in self.default_roots]
        return consult(
            lambda: _open(where, source.session), _info, absent=SessionInfo(), locked=SessionInfo()
        )


def _origin(store: _Store) -> str:
    return store.origin()


def _info(store: _Store) -> SessionInfo:
    return store.info()
