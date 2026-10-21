"""One Claude Code record as a chat turn, or nothing."""

from __future__ import annotations

import json
from typing import Final

from herdr_pwa.engine.claude.records import (
    Block,
    ClaudeRecord,
    Queued,
    ResultBody,
    ResultParts,
    blocks_of,
    claude_record,
    is_human,
    source_of,
)
from herdr_pwa.engine.notices import (
    OUTPUT,
    PRINTED_NOTHING,
    SHELL,
    SLASH,
    SUMMARY,
    SUMMARY_LINE,
    CommandOutput,
    Hidden,
    PeerMessage,
    ShellCommand,
    SlashCommand,
    TaskNotice,
    api_error,
    injected,
    without_paste_tags,
    without_reminders,
)
from herdr_pwa.engine.transcript import (
    ENQUEUE,
    MAX_NOTE_CHARS,
    MAX_PROSE_CHARS,
    MAX_RESULT_CHARS,
    POP_ALL,
    REMOVE,
    TAKEN,
    Speaker,
    TranscriptTurn,
    api_error_turn,
    clip,
    one_line,
    tool_summary,
)


def parse_claude_line(raw: bytes) -> TranscriptTurn | None:
    """One Claude Code JSONL line as a turn, or ``None`` for anything that is not one."""
    record = claude_record(raw)
    return _claude_turn(record) if record is not None else None


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
    human = is_human(record)
    found = injected(record.origin, content, human=human)
    if isinstance(found, Hidden):
        return None
    if found is not None:
        return _injected_turn(found, uuid)
    if record.is_meta:
        # Measured: a scheduled prompt is queued, then delivered hidden; only the queue reads it.
        return _queue_mark(TAKEN, content, uuid) if source_of(record) else None
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
    for block in blocks_of(record):
        turn = _user_block(block, uuid, meta=meta)
        if turn is not None:
            return turn
    return None


def _user_block(block: Block, uuid: str, *, meta: bool) -> TranscriptTurn | None:
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


def _result_text(body: ResultBody) -> str:
    """A tool result as text; an inline image is named, never dumped as its base64."""
    if isinstance(body, str):
        return body
    if isinstance(body, ResultParts):
        parts = body.root
        return "\n".join(part.text or "" if part.type == "text" else "[image]" for part in parts)
    return json.dumps(body, ensure_ascii=False)


def _api_error_turn(record: ClaudeRecord, uuid: str) -> TranscriptTurn:
    """A refused turn, opening on the harness's words."""
    said = "\n".join(block.said for block in blocks_of(record) if block.type == "text")
    return api_error_turn(api_error(record.error, said), uuid)


def _assistant_record(record: ClaudeRecord, uuid: str) -> TranscriptTurn | None:
    """An assistant record: prose, or one collapsed tool call."""
    for block in blocks_of(record):
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


def delivered(record: ClaudeRecord) -> TaskNotice | PeerMessage | None:
    """The task notice or peer message a queued-command attachment delivered, if it is one."""
    queued = record.attachment
    if queued is None or queued.type != _QUEUED:
        return None
    found = injected(queued.origin, queued.prompt or "", human=False)
    # A prompt the person queued is not read here: only what the harness delivered.
    return found if isinstance(found, TaskNotice | PeerMessage) else None


def _queued_prompt(queued: Queued, uuid: str) -> TranscriptTurn | None:
    """A prompt the person typed mid-turn, which the harness writes only as this attachment."""
    if queued.type != _QUEUED or queued.origin is None or queued.origin.kind != "human":
        return None
    text, cut = _prompt(queued.prompt or "")
    if not text:
        return None
    return TranscriptTurn(uuid=uuid, speaker=Speaker.human, text=text, truncated=cut)


def _queued_turn(record: ClaudeRecord, uuid: str) -> TranscriptTurn | None:
    found = delivered(record)
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
