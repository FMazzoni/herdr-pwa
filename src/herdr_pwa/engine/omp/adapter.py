"""omp's session log: one JSONL per session, read as one conversation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import ClassVar, Final, override

from herdr_pwa.engine.notices import api_error
from herdr_pwa.engine.omp.ask import match
from herdr_pwa.engine.omp.pending import ask_stamp, pending_ask
from herdr_pwa.engine.omp.records import (
    KIND,
    OMP_ROOT,
    Block,
    Message,
    Record,
    Usage,
    blocks_of,
    parse_record,
    session_log,
    trimmed,
)
from herdr_pwa.engine.pages import lines_between, read_page, read_since
from herdr_pwa.engine.question import QuestionMatch, RecordedQuestion
from herdr_pwa.engine.transcript import (
    MAX_HISTORY_BYTES,
    MAX_PROSE_CHARS,
    MAX_RESULT_CHARS,
    MAX_USAGE_BYTES,
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
    one_line,
    remember,
    spend_of,
    token_count,
    tool_summary,
)

_HEAD_BYTES: Final = 64 * 1024

# The stop reason a turn ended on with no answer, which the chat says in the harness's words.
_STOP_FAILED: Final = "error"
# Answers a turn never gave: their zeros must not name a model or empty the context gauge.
_STOP_ANSWERLESS: Final = frozenset({"error", "aborted", "interrupted"})


class OmpAdapter(TranscriptAdapter):
    """omp's history: one JSONL per session, paged by the shared byte reader."""

    default_roots: ClassVar[tuple[str, ...]] = (OMP_ROOT,)
    harness: ClassVar[Harness] = Harness(
        KIND,
        StartRecipe("omp"),
        # Measured: the assistant record holding a tool call is in the log while the tool runs.
        records_calls=True,
        # Its Ask dialog marks rows with ``○``, so a numbered run on screen is model text.
        numbers_menus=False,
    )

    @override
    def page(self, source: Source, window: Window) -> TranscriptPage:
        """The *window*'s turns or messages, ending at byte ``before``, oldest first."""
        path = self.log(source)
        if path is None:
            return TranscriptPage(state=HistoryState.absent)
        if window.max_bytes is None and window.messages:
            # Counting messages, the budget must reach past the file's bookkeeping to find them.
            window = replace(window, max_bytes=MAX_HISTORY_BYTES)
        return read_page(path, window, parse=_turns)

    @override
    def since(self, source: Source, window: Window) -> TranscriptPage:
        """Whatever has been appended at or past the *window*'s byte ``after``, oldest first."""
        path = self.log(source)
        return (
            TranscriptPage(state=HistoryState.absent)
            if path is None
            else read_since(path, window, parse=_turns)
        )

    @override
    def tally(self, source: Source, *, max_bytes: int = MAX_USAGE_BYTES) -> SessionUsage:
        """What the session has spent, summed over the assistant records it still holds."""
        path = self.log(source)
        return SessionUsage(state=HistoryState.absent) if path is None else _tally(path, max_bytes)

    @override
    def origin(self, source: Source) -> str:
        """The ``cwd`` the session record names: where omp started it."""
        path = self.log(source)
        return "" if path is None else _origin(path)

    @override
    def pending_question(self, source: Source) -> RecordedQuestion | None:
        """The single-choice question of the log's newest ask call that no result answers."""
        pending = pending_ask(source)
        return None if pending is None else pending.recorded

    @override
    def confirm_question(self, screen: str, question: RecordedQuestion) -> QuestionMatch | None:
        """*question* as omp's closing Ask box draws it, or ``None`` when any line differs."""
        return match(screen, question)

    @override
    def question_stamp(self, source: Source) -> str:
        """The session log's stamp, which moves with every ask and result omp writes."""
        return ask_stamp(source)

    @classmethod
    def log(cls, source: Source) -> Path | None:
        """The session's log in the first root holding it, contained to that root."""
        return session_log(source)


def _turns(at: int, raw: bytes) -> tuple[TranscriptTurn, ...]:
    """Every turn one line holds: omp may batch a prose block and tool calls in one message."""
    record = parse_record(raw)
    if record is None or record.type != "message" or record.message is None:
        return ()
    message = record.message
    uuid = trimmed(record.id) or str(at)
    if message.role == "user":
        return _prompt_turns(message, uuid)
    if message.role == "assistant":
        return _answer_turns(message, uuid)
    if message.role == "toolResult":
        return (_result_turn(message, uuid),)
    return ()


def _prompt_turns(message: Message, uuid: str) -> tuple[TranscriptTurn, ...]:
    """A person's prompt, or the harness's own injected text, as the person's words."""
    text = "\n\n".join(
        block.text.strip() for block in blocks_of(message) if block.type == "text" and block.text
    )
    clipped, cut = clip(text, MAX_PROSE_CHARS)
    return (
        (TranscriptTurn(uuid=uuid, speaker=Speaker.human, text=clipped, truncated=cut),)
        if clipped
        else ()
    )


def _answer_turns(message: Message, uuid: str) -> tuple[TranscriptTurn, ...]:
    """An answer's prose and its calls, in the order the harness wrote them."""
    said = trimmed(message.error_message)
    if message.stop_reason == _STOP_FAILED and said:
        return (api_error_turn(api_error(None, said), uuid),)
    drawn = [block for block in blocks_of(message) if block.type in ("text", "toolCall")]
    found: list[TranscriptTurn] = []
    for index, block in enumerate(drawn):
        # One block is the record, several need their own ids so a page can append either.
        turn = _answer_turn(block, uuid if len(drawn) == 1 else f"{uuid}:{index}")
        if turn is not None:
            found.append(turn)
    return tuple(found)


def _answer_turn(block: Block, key: str) -> TranscriptTurn | None:
    """One prose block or one call, or ``None`` for prose that is only whitespace."""
    if block.type == "text":
        text, cut = clip(block.text or "", MAX_PROSE_CHARS)
        if not text:
            return None
        return TranscriptTurn(uuid=key, speaker=Speaker.agent, text=text, truncated=cut)
    summary, cut = tool_summary(block.arguments)
    if not summary:
        summary, cut = one_line(block.intent)
    return TranscriptTurn(
        uuid=key,
        speaker=Speaker.tool,
        text=summary,
        tool=trimmed(block.name),
        truncated=cut,
        call=trimmed(block.id),
    )


def _result_turn(message: Message, uuid: str) -> TranscriptTurn:
    """A tool's result, as one line that opens on what it printed."""
    text, cut = clip(_result_text(blocks_of(message)), MAX_RESULT_CHARS)
    return TranscriptTurn(
        uuid=uuid,
        speaker=Speaker.tool,
        text=text,
        tool="result",
        truncated=cut,
        call=trimmed(message.tool_call_id),
        failed=message.is_error is True,
    )


def _result_text(blocks: list[Block]) -> str:
    """A tool result as text; an inline image is named, never dumped as its base64."""
    return "\n".join(part for part in map(_result_part, blocks) if part)


def _result_part(block: Block) -> str:
    """One result block as text: its own text, a name for an image, else nothing."""
    if block.type == "text":
        return block.text or ""
    return "[image]" if block.type == "image" else ""


def _tally(path: Path, max_bytes: int) -> SessionUsage:
    """What the session has spent, one message record at a time."""
    tally = SessionUsage()
    size = path.stat().st_size
    start = max(0, size - max_bytes)
    tally.partial = start > 0
    for _, raw in lines_between(path, start, size).items:
        _fold_record(tally, parse_record(raw))
    # A prompt alone proves the session was used, even before any answer's usage landed.
    tally.state = HistoryState.ok if tally.messages or tally.prompts else HistoryState.empty
    return tally


def _fold_record(tally: SessionUsage, record: Record | None) -> None:
    """Fold one record's time, and a message's prompts or spend, into *tally*."""
    if record is None:
        return
    stamp = trimmed(record.timestamp)
    if stamp:
        tally.last_at = stamp
    message = record.message
    if record.type != "message" or message is None:
        return
    if message.role == "user":
        tally.prompts += 1
        tally.turn_started = stamp or tally.turn_started
    elif message.role == "assistant":
        _take(tally, message)


def _take(tally: SessionUsage, message: Message) -> None:
    """Fold one assistant record into *tally*: its model, tokens and money."""
    usage = message.usage
    if usage is None:
        return
    cost = _cost(usage)
    # A refused or aborted turn names a model that never answered, and zeros would empty the gauge.
    if message.stop_reason in _STOP_ANSWERLESS:
        tally.cost += cost
        return
    ref = ModelRef(trimmed(message.model), trimmed(message.provider))
    _add(tally, usage, cost)
    remember(tally, ref)
    row = spend_of(tally, ref)
    if row is not None:
        _add(row, usage, cost)
    # What went up with the newest answer: fresh, cache-written and cache-read tokens.
    tally.context_tokens = (
        token_count(usage.input) + token_count(usage.cache_write) + token_count(usage.cache_read)
    )


def _add(spend: Spend, usage: Usage, cost: float) -> None:
    """Fold one answer's tokens and money into *spend*, in place."""
    spend.messages += 1
    spend.input_tokens += token_count(usage.input)
    spend.output_tokens += token_count(usage.output)
    spend.thinking_tokens += token_count(usage.reasoning)
    spend.cache_creation_tokens += token_count(usage.cache_write)
    spend.cache_read_tokens += token_count(usage.cache_read)
    spend.cost += cost


def _cost(usage: Usage) -> float:
    """One answer's recorded money; a provider that wrote none has no price to invent."""
    total = usage.cost.total if usage.cost is not None else None
    return float(total) if total is not None and total > 0 else 0.0


def _origin(path: Path) -> str:
    """The cwd in the session record, which omp writes before anything else."""
    size = path.stat().st_size
    for _, raw in lines_between(path, 0, min(size, _HEAD_BYTES)).items:
        record = parse_record(raw)
        if record is not None and record.type == "session":
            return trimmed(record.cwd)
    return ""
