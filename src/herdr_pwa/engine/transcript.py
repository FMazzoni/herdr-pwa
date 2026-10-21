"""The provider transcript: the only source that is a conversation, not a screen."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Final, NamedTuple, Protocol

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from herdr_pwa.engine.generated.request import IntegrationTarget
from herdr_pwa.engine.model import Agent, SessionId
from herdr_pwa.engine.notices import (
    API_ERROR_KINDS,
    COMPACT_COMMAND,
    OUTPUT,
    PRINTED_NOTHING,
    SHELL,
    SLASH,
    SLASH_KINDS,
    SUMMARY,
    ApiError,
)
from herdr_pwa.validation import Text

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

    # What the harness's prompt queue did: never drawn, only read for what is still waiting.
    queue = "queue"


class TranscriptTurn(BaseModel):
    """One renderable line of conversation."""

    uuid: str
    speaker: Speaker
    text: str
    tool: str = ""
    truncated: bool = False
    # The tool_use id, carried by a call and by its result: the only thing pairing the two.
    call: str = ""
    result: str = ""
    failed: bool = False
    result_truncated: bool = False
    # The frontier just before this turn: `before=at` leaves it out, `after=at` starts with it.
    at: int = -1
    # How a task notice's task ended ("exit 0") and how long it ran; its kind rides in `tool`.
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
            ()
            if adapter is None
            else tuple(search_roots(root, kind=kind, default=adapter.default_roots))
        )
        # A session id off a URL segment must not walk the tree, so a path reads as no session.
        return cls(session_name(session), roots, expect=log, kind=kind)


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
    # Prompts the person sent that the harness holds and the agent has not taken yet.
    queued: list[str] = Field(default_factory=list)
    # The file actually read, which is not the named session once a fork was followed.
    log: str = ""


class ToolInput(BaseModel):
    """A tool call's arguments, as far as naming the call goes, however a harness spells them."""

    model_config = ConfigDict(extra="ignore")

    file_path: Text = Field(default=None, validation_alias=AliasChoices("file_path", "filePath"))
    notebook_path: Text = None
    path: Text = None
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


# Locating the file


def _for_kind(entry: str, kind: str) -> str:
    """*entry* as it applies to *kind*: unqualified, qualified for it, or empty."""
    head, sep, rest = entry.partition(":")
    if not sep or head.strip().lower() not in _registry():
        return entry
    return rest.strip() if head.strip().lower() == kind else ""


def search_roots(
    root: TranscriptRoots = None,
    *,
    kind: str = "claude",
    default: Sequence[str],
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


def session_name(session: SessionId | str) -> str:
    """*session* as a NAME, or ``""`` for anything that is really a path."""
    # A session id off a URL segment must not walk the tree.
    text = str(session).strip()
    if not text or "/" in text or "\\" in text or text.startswith("."):
        return ""
    return text


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


def api_error_turn(found: ApiError, uuid: str) -> TranscriptTurn:
    """A turn that ended with no answer as a quiet line saying what unblocks it."""
    line, _ = one_line(found.line)
    result, cut = clip(found.said, MAX_RESULT_CHARS)
    return TranscriptTurn(
        uuid=uuid,
        speaker=Speaker.notice,
        text=line,
        tool=found.kind,
        result="" if result == line else result,
        result_truncated=cut,
        failed=found.failed,
    )


# Measured queue verbs: sent while busy, absorbed or taken back, pulled back to edit, a bare pop.
ENQUEUE: Final = "enqueue"
REMOVE: Final = "remove"
POP_ALL: Final = "popAll"
TAKEN: Final = "taken"


def fold_turns(turns: Iterable[TranscriptTurn]) -> list[TranscriptTurn]:
    """Fold results onto calls, output and compactions onto commands, and repeated API errors."""
    said = (turn for turn in turns if turn.speaker is not Speaker.queue)
    return _fold_repeats(_fold_compaction(_fold_outputs(_fold_results(said))))


def fold_page(page: TranscriptPage) -> TranscriptPage:
    """*page* with its turns folded and the prompts still waiting in the queue taken out of them."""
    return page.model_copy(update={"turns": fold_turns(page.turns), "queued": waiting(page.turns)})


# Compared on the words up to the shortest cap a delivery is clipped to.
_SAME_PROMPT: Final = MAX_NOTE_CHARS


def _delivers(turn: TranscriptTurn) -> bool:
    """Whether *turn* is a prompt reaching the agent, which is what ends its wait in the queue."""
    if turn.speaker is Speaker.queue:
        return turn.tool in (REMOVE, TAKEN)
    return turn.speaker in (Speaker.human, Speaker.system) or (
        is_slash_command(turn) and turn.tool == SLASH
    )


def waiting(turns: Iterable[TranscriptTurn]) -> list[str]:
    """The prompts queued in *turns* not since delivered, removed or pulled back, by words."""
    held: list[str] = []
    for turn in turns:
        if turn.speaker is Speaker.queue and turn.tool == ENQUEUE:
            held.append(turn.text)
        elif turn.speaker is Speaker.queue and turn.tool == POP_ALL:
            held.clear()
        elif _delivers(turn):
            _take_first(held, turn.text)
    return held


def _take_first(held: list[str], said: str) -> None:
    """Drop the oldest of *held* that *said* delivers, if any does."""
    key = said[:_SAME_PROMPT]
    match = next((i for i, text in enumerate(held) if text[:_SAME_PROMPT] == key), None)
    if match is not None:
        del held[match]


def _prints_for(output: TranscriptTurn, command: TranscriptTurn) -> bool:
    """Whether *output* is what *command* printed: measured, it is the very next record."""
    ran = (is_slash_command(command) and command.tool == SLASH) or (
        command.speaker is Speaker.human and command.tool == SHELL
    )
    return is_slash_command(output) and output.tool == OUTPUT and ran and not command.result


def _fold_outputs(turns: list[TranscriptTurn]) -> list[TranscriptTurn]:
    """Each slash command's OUTPUT folded onto the command just before it, where it opens."""
    folded: list[TranscriptTurn] = []
    for turn in turns:
        if folded and _prints_for(turn, folded[-1]):
            update = {
                "result": turn.result,
                "result_truncated": turn.result_truncated,
                "failed": turn.failed,
                "outcome": turn.outcome,
            }
            folded[-1] = folded[-1].model_copy(update=update)
            continue
        # Nothing printed, by a command not on the page, is nothing to draw.
        if not _printed_nothing(turn):
            folded.append(turn)
    return folded


def _printed_nothing(turn: TranscriptTurn) -> bool:
    return is_slash_command(turn) and turn.tool == OUTPUT and turn.outcome == PRINTED_NOTHING


def _runs_compact(turn: TranscriptTurn) -> bool:
    return (
        is_slash_command(turn) and turn.tool == SLASH and turn.text.split(" ")[0] == COMPACT_COMMAND
    )


# Mutable: the fold fills it in as it walks the turns.
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
    """Each /compact line on its summary, the line as sent dropped; a typed prompt ends it."""
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
    # The ``Settings`` field naming models for a harness that can't list its own; empty: ``models``.
    models_setting: str = ""
    env: Mapping[str, str] = field(default_factory=dict[str, str])
    # The flag giving an alternate-screen TUI its first prompt, so no shell echo reads as landed.
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
    # One append-only JSONL per session, paged by byte, so the caches grow by what was appended.
    byte_frontier: bool = False
    # Whether the tally's context figure is the prompt's size, rather than nothing stored.
    stores_context: bool = True
    # Whether the tally's cost is the harness's own price; one recording no money reads zero.
    prices_itself: bool = True


# What the harness delivers between retries: each one sets off the next refused turn.
_RETRY_TRIGGERS: Final = (Speaker.notice, Speaker.peer)


def _wakes(turn: TranscriptTurn) -> bool:
    """Whether *turn* only woke the agent to retry; a slash command is the person acting."""
    return turn.speaker in _RETRY_TRIGGERS and not is_slash_command(turn)


def _same_error(a: TranscriptTurn, b: TranscriptTurn) -> bool:
    return (a.tool, a.text, a.result) == (b.tool, b.text, b.result)


def _fold_repeats(turns: list[TranscriptTurn]) -> list[TranscriptTurn]:
    """Each API error folded into the identical one before it, across the wake-ups between them."""
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
    """The API error the conversation ends on, past what only woke it to retry; else ``None``."""
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


_adapters: Final[dict[str, TranscriptAdapter]] = {}


def _registry() -> dict[str, TranscriptAdapter]:
    """The adapters, built on first use."""
    if not _adapters:
        # The adapters import this module, so importing them at the top would be a cycle.
        from herdr_pwa.engine.claude.adapter import ClaudeAdapter  # noqa: PLC0415 - the adapters import this module, so a top import is a cycle
        from herdr_pwa.engine.hermes import HermesAdapter  # noqa: PLC0415 - the adapters import this module, so a top import is a cycle
        from herdr_pwa.engine.opencode import OpencodeAdapter  # noqa: PLC0415 - the adapters import this module, so a top import is a cycle

        adapters: tuple[TranscriptAdapter, ...] = (
            ClaudeAdapter(),
            OpencodeAdapter(),
            HermesAdapter(),
        )
        _adapters.update({adapter.harness.kind: adapter for adapter in adapters})
    return _adapters


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
    return fold_page(page)


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


# What a session started in the background: another agent, or a shell running a command.
SPAWNED_AGENT: Final = "agent"
SPAWNED_SHELL: Final = "shell"


class SpawnedAgent(BaseModel):
    """Work the session started in the background, an agent or a shell, by its task id."""

    task: str
    kind: str = SPAWNED_AGENT
    description: str = ""
    started: str = ""
    # When a notice said it was over; a notice that it waits on its own work does not count.
    done: bool = False
    ended: str = ""
    failed: bool = False
    # When a message last woke it after it ended; it runs again until a later ending.
    resumed: str = ""


class SessionUsage(Spend):
    """What one session has spent over every log its thread lived in, and its background agents."""

    # The newest message's whole prompt: how full the window is now, not a total.
    context_tokens: int = 0
    models: list[ModelRef] = Field(default_factory=list)
    # The model of the newest answer: a session that went A, B, A again is on A, not B.
    latest: ModelRef = ModelRef()
    # Only where the store says which model each answer came from; empty means not split.
    by_model: list[ModelSpend] = Field(default_factory=list)
    # A byte budget stopped the read, so the sums are floors.
    partial: bool = False
    state: HistoryState = HistoryState.absent
    # Prompts the person sent, counted once however many logs a fork copied them into.
    prompts: int = 0
    # When the newest prompt was sent, and when the newest record landed; empty when unknown.
    turn_started: str = ""
    last_at: str = ""
    agents: list[SpawnedAgent] = Field(default_factory=list[SpawnedAgent])


def token_count(value: int | None) -> int:
    """One usage field as a non-negative int, whatever the provider actually wrote."""
    return value if value is not None and value > 0 else 0


def remember(tally: SessionUsage, ref: ModelRef) -> None:
    """Record *ref* as the newest to answer, and in *tally*'s models once, in first-seen order."""
    if not ref.model:
        return
    tally.latest = ref
    if ref not in tally.models:
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
